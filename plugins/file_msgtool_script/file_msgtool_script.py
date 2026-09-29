"""msg-tool 文件插件。

借助 `res/msg_tool.exe`（lifegpc/msg-tool）直接读写 galgame 脚本文件：

- ``load_file``：调用 ``msg-tool export -T json`` 把脚本导出为 GalTransl 的
  ``[{"name"?, "message"}]`` JSON，并补充 ``index`` / ``org_message`` 等附加字段；
- ``save_file``：把译文还原成 JSON，再调用 ``msg-tool import`` 回填到脚本副本，
  最后写入 GalTransl 给定的 gt_output 路径。

msg-tool 的 json 输出类型本身就是 GalTransl 格式，因此不需要任何格式转换。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading

from GalTransl import LOGGER, INPUT_FOLDERNAME, OUTPUT_FOLDERNAME
from GalTransl.GTPlugin import GFilePlugin

# 兼容旧版项目的目录名（见 GalTransl/ConfigHelper.py）
_LEGACY_INPUT_DIRNAMES = ("json_jp",)
_LEGACY_OUTPUT_DIRNAMES = ("json_cn",)

# 不同插件实例也会被 HTTP 预览接口反复创建；进程内按路径合并同时读取。
_CACHE_LOCKS = tuple(threading.Lock() for _ in range(64))
_CACHE_VERSION = 1


class file_plugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        在插件加载时被调用。
        :param plugin_conf: 插件yaml中所有设置的dict。
        :param project_conf: 项目yaml中common下设置的dict（含 project_dir）。
        """
        self.pname = plugin_conf["Core"].get("Name", "file_msgtool_script")
        settings = plugin_conf.get("Settings") or {}

        self.script_type = str(settings.get("script_type") or "").strip()
        self.source_encoding = str(settings.get("source_encoding") or "").strip()
        self.patched_encoding = str(settings.get("patched_encoding") or "").strip()
        self.extra_args = [str(a) for a in (settings.get("extra_args") or [])]
        self.keep_bilingual = bool(settings.get("keep_bilingual", False))
        self.bilingual_sep = str(settings.get("bilingual_sep", "\n"))
        try:
            self.timeout = int(settings.get("timeout", 300) or 300)
        except (TypeError, ValueError):
            self.timeout = 300

        self.project_dir = str(project_conf.get("project_dir") or settings.get("project_dir") or "")
        self.input_dir = self._project_folder(INPUT_FOLDERNAME, _LEGACY_INPUT_DIRNAMES)
        self.output_dir = self._project_folder(OUTPUT_FOLDERNAME, _LEGACY_OUTPUT_DIRNAMES)
        self.msg_tool = self._resolve_msg_tool(settings.get("msg_tool_path"))
        self.read_cache = bool(settings.get("read_cache", True))
        self.cache_dir = os.path.join(
            os.path.abspath(self.project_dir) if self.project_dir else tempfile.gettempdir(),
            ".galtransl-cache", "msgtool",
        )

        # load_file 时记录「gt_output 路径 -> gt_input 原始脚本路径」，供 save_file 找回原文件
        self._src_by_out: dict[str, str] = {}

        LOGGER.debug(
            f"[{self.pname}] msg_tool={self.msg_tool} "
            f"script_type={self.script_type or 'auto'} "
            f"patched_encoding={self.patched_encoding or 'default'} "
            f"extra_args={self.extra_args}"
        )

    # ------------------------------------------------------------------ #
    # 路径与可执行文件
    # ------------------------------------------------------------------ #
    def _resolve_msg_tool(self, configured) -> str:
        """定位 msg-tool 可执行文件：配置优先，其次程序目录下的 res/msg_tool.exe。"""
        here = os.path.dirname(os.path.abspath(__file__))
        default = os.path.abspath(
            os.path.join(here, "..", "..", "res", "msg_tool.exe")
        )
        candidates = []
        if configured:
            candidates.append(os.path.abspath(str(configured)))
        candidates.append(default)
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        # 都找不到时返回首选路径，错误信息里能直接看到期望的位置
        return candidates[0]

    def _project_folder(self, current: str, legacy: tuple[str, ...]) -> str:
        """与 ConfigHelper 一致，独立选择输入和输出目录，并在初始化时固定。"""
        if not self.project_dir:
            return ""
        primary = os.path.abspath(os.path.join(self.project_dir, current))
        if not os.path.exists(primary):
            for name in legacy:
                candidate = os.path.abspath(os.path.join(self.project_dir, name))
                if os.path.exists(candidate):
                    return candidate
        return primary

    @staticmethod
    def _relative_path(file_path: str, directory: str) -> str | None:
        if not directory:
            return None
        try:
            rel = os.path.relpath(os.path.abspath(file_path), directory)
        except ValueError:  # Windows 不同盘符
            return None
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            return None
        return rel

    def _output_path_for(self, input_path: str) -> str:
        """由 gt_input 下的路径推出 GalTransl 传给 save_file 的 gt_output 路径。"""
        rel = self._relative_path(input_path, self.input_dir)
        return os.path.join(self.output_dir, rel) if rel is not None else ""

    def _source_path_for(self, output_path: str) -> str | None:
        """save_file 收到 gt_output 路径，反推出对应的 gt_input 原始脚本路径。"""
        output_abs = os.path.abspath(output_path)
        cached = self._src_by_out.get(output_abs)
        if cached and os.path.isfile(cached):
            return cached

        rel = self._relative_path(output_abs, self.output_dir)
        if rel is not None:
            src = os.path.join(self.input_dir, rel)
            if os.path.isfile(src):
                return src
        return None

    # ------------------------------------------------------------------ #
    # 调用 msg-tool
    # ------------------------------------------------------------------ #
    def _run(self, args: list) -> str:
        if not os.path.isfile(self.msg_tool):
            raise RuntimeError(
                f"找不到 msg-tool 可执行文件：{self.msg_tool}"
                f"（可修改插件配置 msg_tool_path）"
            )
        # -x/-X：让 msg-tool 在任务失败时返回非 0 退出码，便于这里判断成败
        cmd = [self.msg_tool, "-x", "1", "-X", "2"] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"msg-tool 执行超时（{self.timeout}s）：{' '.join(cmd)}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"启动 msg-tool 失败：{exc}") from exc

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            detail = (stderr.strip() or stdout.strip() or "(无输出)").replace("\n", " ")
            raise RuntimeError(
                f"msg-tool 执行失败（exit={proc.returncode}）：{detail}"
            )
        diagnostic = stdout + "\n" + stderr
        # msg-tool 的编码丢失只产生 Warning，退出码仍为 0。
        # 必须在复制临时输出之前失败，保留原脚本和已有译文。
        if re.search(r"(?im)^\s*Warning:.*could not be encoded", diagnostic):
            raise RuntimeError(
                "msg-tool 回填发生编码丢失，已阻止写出；请将 patched_encoding "
                "设置为游戏支持的编码（如 gb2312 或 utf8）后重建输出。\n"
                + diagnostic.strip()
            )
        if re.search(r"(?im)^\s*Warning:", diagnostic):
            LOGGER.warning(f"[{self.pname}] {diagnostic.strip()}")
        return diagnostic

    def _has_extra_option(self, *options: str) -> bool:
        return any(
            arg == option or arg.startswith(option + "=")
            or (len(option) == 2 and arg.startswith(option) and len(arg) > 2)
            for arg in self.extra_args for option in options
        )

    def _common_args(self, source_path: str) -> list:
        args = []
        script_type = self.script_type
        if not script_type and os.path.splitext(source_path)[1].lower() == ".ks":
            script_type = "kirikiri"
        if script_type and not self._has_extra_option("-t", "--script-type"):
            args += ["-t", script_type]
        encoding = self.source_encoding or ("auto" if script_type == "kirikiri" else "")
        if encoding and not self._has_extra_option("-e", "--encoding", "-c", "--code-page"):
            args += ["-e", encoding]
        args += self.extra_args
        return args

    # ------------------------------------------------------------------ #
    # GalTransl 文件插件接口
    # ------------------------------------------------------------------ #
    def _cache_fingerprint(self, file_path: str) -> str:
        """源内容、解析配置、工具版本以及同目录依赖变化均使缓存失效。"""
        source_hash = hashlib.sha256()
        with open(file_path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                source_hash.update(block)
        tool_stat = os.stat(self.msg_tool)
        siblings = []
        # 某些二进制格式需要同目录的元数据文件，不能只检查脚本自身。
        with os.scandir(os.path.dirname(os.path.abspath(file_path))) as entries:
            for entry in entries:
                if entry.is_file():
                    stat = entry.stat()
                    siblings.append((entry.name, stat.st_size, stat.st_mtime_ns))
        identity = [
            _CACHE_VERSION, source_hash.hexdigest(), self._common_args(file_path),
            os.path.normcase(os.path.abspath(self.msg_tool)),
            tool_stat.st_size, tool_stat.st_mtime_ns, sorted(siblings),
        ]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    @staticmethod
    def _valid_cached_rows(rows) -> bool:
        return isinstance(rows, list) and all(
            isinstance(row, dict)
            and isinstance(row.get("message"), str)
            and row.get("org_message") == row["message"]
            and row.get("index") == index
            and ("name" not in row or isinstance(row["name"], str))
            for index, row in enumerate(rows, 1)
        )

    def _load_cached(self, file_path: str) -> list:
        # extra_args 可以引用外部文件/目录或要求导出附加文件；这时必须实际调用工具。
        if not self.read_cache or self.extra_args:
            return self._load_uncached(file_path)
        path_key = hashlib.sha256(os.path.normcase(os.path.abspath(file_path)).encode()).hexdigest()
        cache_path = os.path.join(self.cache_dir, path_key + ".json")
        with _CACHE_LOCKS[int(path_key[:8], 16) % len(_CACHE_LOCKS)]:
            try:
                fingerprint = self._cache_fingerprint(file_path)
            except OSError:
                return self._load_uncached(file_path)
            try:
                with open(cache_path, "r", encoding="utf-8") as cache:
                    payload = json.load(cache)
                if (isinstance(payload, dict) and payload.get("fingerprint") == fingerprint
                        and self._valid_cached_rows(payload.get("rows"))):
                    LOGGER.debug(f"[{self.pname}] 提取缓存命中：{file_path}")
                    return payload["rows"]
            except (OSError, ValueError):
                pass  # 缺失、损坏或不可读的缓存均重新提取。

            rows = self._load_uncached(file_path)
            temporary = None
            try:
                # 提取期间有改动时不缓存，避免以旧指纹保存新旧混合的结果。
                if self._cache_fingerprint(file_path) != fingerprint:
                    return rows
                os.makedirs(self.cache_dir, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.cache_dir, suffix=".tmp", delete=False,
                ) as cache:
                    temporary = cache.name
                    json.dump({"fingerprint": fingerprint, "rows": rows}, cache, ensure_ascii=False)
                os.replace(temporary, cache_path)
            except OSError as exc:
                LOGGER.debug(f"[{self.pname}] 无法写入提取缓存：{exc}")
            finally:
                if temporary and os.path.exists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            return rows

    def load_file(self, file_path: str) -> list:
        """默认复用持久化提取缓存，每次返回独立列表供翻译流程修改。"""
        if not os.path.isfile(file_path):
            raise TypeError(f"文件不存在：{file_path}")
        rows = self._load_cached(file_path)
        out_path = self._output_path_for(file_path)
        if out_path:
            self._src_by_out[out_path] = os.path.abspath(file_path)
        return rows

    def _load_uncached(self, file_path: str) -> list:
        """
        加载脚本文件，返回 [{message, index, org_message, name?}, ...]。
        """
        if not os.path.isfile(file_path):
            raise TypeError(f"文件不存在：{file_path}")

        export_type = "json"
        with tempfile.TemporaryDirectory(prefix="gt_msgtool_") as tmp_dir:
            out_json = os.path.join(tmp_dir, "export.json")
            self._run(
                ["export", "-T", export_type] + self._common_args(file_path) + [file_path, out_json]
            )
            if not os.path.isfile(out_json):
                # msg-tool 判定该脚本不含任何文本（Ignored），视为空文件
                LOGGER.warning(f"[{self.pname}] {file_path} 未提取到任何文本，跳过")
                return []
            with open(out_json, "r", encoding="utf-8") as f:
                raw_list = json.load(f)

        if not isinstance(raw_list, list):
            raise RuntimeError(f"msg-tool 导出的 {export_type} 不是列表：{file_path}")

        result = []
        for i, item in enumerate(raw_list):
            if not isinstance(item, dict):
                raise RuntimeError(f"msg-tool 导出的第 {i + 1} 项不是对象：{item!r}")
            message = item.get("message")
            if not isinstance(message, str):
                raise RuntimeError(f"msg-tool 导出的第 {i + 1} 项缺少 message 字段：{item!r}")
            row = {"message": message, "index": i + 1, "org_message": message}
            name = item.get("name")
            if isinstance(name, str) and name:
                row["name"] = name
            result.append(row)
        return result

    def save_file(self, file_path: str, transl_json: list):
        """
        把译文回填到脚本并写入 gt_output。
        :param file_path: GalTransl 给定的输出路径（gt_output 下）。
        :param transl_json: load_file 返回的列表在翻译 message/name 之后的结果。
        """
        output_path = os.path.abspath(file_path)
        source_path = self._source_path_for(output_path)
        if not source_path:
            raise RuntimeError(
                f"找不到 {file_path} 对应的原始脚本（gt_input），无法回填；"
                f"当前 project_dir={self.project_dir or '(空)'}"
            )

        rows = []
        for item in transl_json:
            if not isinstance(item, dict):
                raise RuntimeError(f"待回填的数据项不是对象：{item!r}")
            message = item.get("message")
            if not isinstance(message, str):
                message = "" if message is None else str(message)
            if self.keep_bilingual:
                org = item.get("org_message")
                if isinstance(org, str) and org:
                    message = f"{message}{self.bilingual_sep}{org}"

            row = {}
            name = item.get("name")
            if isinstance(name, list):
                name = next((n for n in name if n), "")
            if isinstance(name, str) and name:
                row["name"] = name
            row["message"] = message
            rows.append(row)

        with tempfile.TemporaryDirectory(prefix="gt_msgtool_") as tmp_dir:
            trans_json = os.path.join(tmp_dir, "trans.json")
            with open(trans_json, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)

            suffix = os.path.splitext(source_path)[1]
            patched_path = os.path.join(tmp_dir, "patched" + suffix)

            import_args = ["import"]
            if self.patched_encoding:
                import_args += ["-p", self.patched_encoding]
            import_args += self._common_args(source_path)
            import_args += [source_path, trans_json, patched_path]
            self._run(import_args)

            if not os.path.isfile(patched_path):
                raise RuntimeError(f"msg-tool 未生成回填后的脚本：{source_path}")

            out_dir = os.path.dirname(output_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            shutil.copyfile(patched_path, output_path)

    def gtp_final(self):
        """所有文件翻译完成之后的动作。"""
        self._src_by_out.clear()
