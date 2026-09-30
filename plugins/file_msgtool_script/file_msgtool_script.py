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
        self.jis_substitution = bool(settings.get("jis_substitution", False))
        self.jis_unmapped = str(settings.get("jis_unmapped", "error"))
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
        self._jis_lock = threading.Lock()
        self._jis_files: dict[str, dict] = {}
        self._jis_previous: dict | None = None
        self._jis_dictionary = {}
        self._jis_targets = set()
        if self.jis_substitution:
            if self.jis_unmapped not in ("error", "space"):
                raise ValueError("jis_unmapped 必须为 error 或 space")
            if self._has_extra_option("-p", "--patched-encoding", "-P", "--patched-code-page"):
                raise ValueError(
                    "JIS 替换固定使用 CP932，请移除 extra_args 中的输出编码参数 "
                    "-p/--patched-encoding/-P/--patched-code-page"
                )
            with open(self._jis_resource("subs_cn_jp.json"), encoding="utf-8") as resource:
                self._jis_dictionary = json.load(resource)
            self._jis_targets = set(self._jis_dictionary.values())

        LOGGER.debug(
            f"[{self.pname}] msg_tool={self.msg_tool} "
            f"script_type={self.script_type or 'auto'} "
            f"patched_encoding={'cp932 (JIS)' if self.jis_substitution else self.patched_encoding or 'default'} "
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
        diagnostic = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
        encoding_warning = re.search(r"(?im)^\s*Warning:.*could not be encoded", diagnostic)
        # CST 等格式严格编码时直接返回 Error；KAG 等格式可能只给 Warning。
        # 两种情况都必须在复制临时输出之前失败，且同时检查 stdout / stderr。
        encoding_error = re.search(
            r"(?i)(?:Failed to encode|Some characters could not be encoded in)\s+"
            r"(Shift[- ]JIS|CP932|GB2312|GBK|UTF-?\d+(?:LE|BE)?|code page\s+\d+)",
            diagnostic,
        )
        if args and args[0] == "import" and (proc.returncode != 0 or encoding_warning) and encoding_error:
            encoding = self._encoding_codec(encoding_error.group(1))
            samples = []
            source_path = args[-3] if len(args) >= 4 else "（未知文件）"
            if encoding and len(args) >= 4:
                try:
                    with open(args[-2], encoding="utf-8") as resource:
                        samples = self._encoding_samples(json.load(resource), encoding)
                except (OSError, ValueError):
                    pass
            raise RuntimeError(self._encoding_failure(
                source_path, encoding or encoding_error.group(1), samples,
                diagnostic=f"msg-tool exit={proc.returncode}\n{diagnostic[:4000]}",
            ))
        if proc.returncode != 0:
            raise RuntimeError(
                f"msg-tool 执行失败（exit={proc.returncode}）：{diagnostic or '(无输出)'}"
            )
        if encoding_warning:
            if not args or args[0] != "import":
                raise RuntimeError("msg-tool 导出文本时发生编码丢失：\n" + diagnostic[:4000])
            raise RuntimeError(self._encoding_failure(
                args[-3] if len(args) >= 4 else "（未知文件）", "工具指定编码", [],
                diagnostic=diagnostic[:4000],
            ))
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

    @staticmethod
    def _option_value(args: list[str], *options: str) -> str | None:
        for index, arg in enumerate(args):
            for option in options:
                if arg == option:
                    return args[index + 1] if index + 1 < len(args) else None
                if arg.startswith(option + "="):
                    return arg[len(option) + 1:]
                if len(option) == 2 and arg.startswith(option) and len(arg) > 2:
                    return arg[2:]
        return None

    @staticmethod
    def _encoding_codec(value: str) -> str | None:
        # msg-tool 的 gb2312 实际使用 GBK；不能用 Python 的 gb2312 子集预检。
        return {
            "cp932": "cp932", "shift-jis": "cp932", "shift jis": "cp932",
            "code page 932": "cp932", "gb2312": "gbk", "gbk": "gbk",
            "code page 936": "gbk", "utf8": "utf-8", "utf-8": "utf-8",
            "auto": "utf-8", "code page 65001": "utf-8",
        }.get(value.lower())

    def _preflight_encoding(self, source_path: str, rows: list, import_args: list):
        """只对已确认编码规则的 CST/KAG 预检；其他格式以工具的诊断为准。"""
        # 工具内置的替换表/人名表可能先把中文改成可编码文本，不能提前拦截。
        if self._option_value(import_args, "--replacement-json", "--name-csv") is not None:
            return
        script_type = self._option_value(import_args, "-t", "--script-type")
        with open(source_path, "rb") as source:
            header = source.read(8)
        if not script_type and header == b"CatScene":
            script_type = "cat-system"
        if script_type not in ("cat-system", "kirikiri"):
            return
        if script_type == "kirikiri":
            # KAG 会保留 Unicode BOM；SimpleCrypt / MDF 的内部编码由工具处理。
            if header.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf", b"\xfe\xfe", b"mdf\0")):
                return
        value = self._option_value(import_args, "-p", "--patched-encoding")
        code_page = self._option_value(import_args, "-P", "--patched-code-page")
        # Windows 代码页允许 best-fit 转换，不用 Python 严格编码去拦截它。
        if code_page is not None:
            return
        codec = self._encoding_codec(value or "cp932") if value != "default" else "cp932"
        if codec in ("cp932", "gbk"):
            samples = self._encoding_samples(rows, codec)
            if samples:
                raise RuntimeError(self._encoding_failure(source_path, codec, samples, preflight=True))

    @staticmethod
    def _encoding_samples(rows: list, codec: str) -> list[str]:
        samples = []
        if not isinstance(rows, list):
            return samples
        # Rust encoding 的兼容映射比 Python 略宽；这些字符由工具继续验证。
        compatible = {"cp932": {"¥", "‾"}, "gbk": {"€"}}.get(codec, set())
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            for field, label in (("name", "人名"), ("message", "正文")):
                text = row.get(field)
                if not isinstance(text, str):
                    continue
                try:
                    text.encode(codec)
                except UnicodeEncodeError:
                    bad = []
                    for char in dict.fromkeys(text):
                        if char in compatible:
                            continue
                        try:
                            char.encode(codec)
                        except UnicodeEncodeError:
                            bad.append(f"{char!r} (U+{ord(char):04X})")
                        if len(bad) == 8:
                            break
                    if bad:
                        samples.append(f"第 {index} 条{label}（{field}）：" + "、".join(bad))
                        if len(samples) == 5:
                            return samples
        return samples

    def _encoding_failure(self, source_path: str, encoding: str, samples: list[str], *,
                          preflight: bool = False, diagnostic: str = "") -> str:
        label = {"cp932": "CP932 / Shift-JIS", "gbk": "GBK（msg-tool 的 gb2312）"}.get(encoding, encoding)
        lines = [
            f"{'回填前编码检查未通过' if preflight else '脚本回填编码失败'}：{source_path}",
            f"输出编码 {label} 无法表示待写入的部分字符，已阻止写出，原脚本和已有输出未修改。",
        ]
        if samples:
            lines.append("无法编码的字符示例（最多 5 处，每处 8 种字符）：")
            lines.extend(samples)
        if encoding == "cp932" and not self.jis_substitution:
            lines.append(
                "游戏需要保持日文编码时：在「Galgame脚本文件」插件设置中开启「JIS 替换」"
                "（jis_substitution），并配合生成的 uif_config.json 与 UIF 或对应替换字体还原显示。"
            )
        elif self.jis_substitution:
            lines.append("当前已开启 JIS 替换；请检查上述字符、脚本保留内容及额外工具参数是否仍引入不可编码字符。")
        lines.extend([
            "游戏支持其他编码时：修改插件的「输出编码」（patched_encoding），例如简体中文 GB2312"
            "（gb2312）或 UTF-8（utf8），需与游戏支持的编码一致。",
            "仅修改「原文编码」（source_encoding）不会改变回填编码；若 extra_args 中设置了 "
            "-p/--patched-encoding 或 -P/--patched-code-page，请同步修改或移除。",
            "也可修改提示中的字符。保存设置或译文后重新构建结果即可，无需重新翻译。",
        ])
        if diagnostic:
            lines.append("原始诊断：\n" + diagnostic)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # SExtractor 兼容的 JIS 替换（字典和 UIF 模板随插件分发）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _jis_resource(name: str) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

    def _substitute_jis(self, rows: list) -> dict:
        """只转换回填副本，保留翻译缓存中的中文；映射方向为中文 -> JIS。"""
        used = {}
        remain = set()
        repeat = set()
        for row in rows:
            for key in ("name", "message"):
                if key not in row:
                    continue
                converted = []
                for char in row[key]:
                    if char in self._jis_targets and char != "―":
                        repeat.add(char)
                    try:
                        char.encode("cp932")
                    except UnicodeEncodeError:
                        replacement = self._jis_dictionary.get(char)
                        if replacement is None:
                            remain.add(char)
                            replacement = "　"
                        else:
                            used[char] = replacement
                        converted.append(replacement)
                    else:
                        converted.append(char)
                row[key] = "".join(converted)
        if remain and self.jis_unmapped == "error":
            raise RuntimeError(
                "JIS 替换字典未覆盖以下字符，已阻止写出：" + "".join(sorted(remain))
                + "。请修改译文，或将 jis_unmapped 设置为 space（替换为全角空格）。"
            )
        return {"used": used, "remain": remain, "repeat": repeat}

    def _save_jis_output(self, patched_path: str, output_path: str, stats: dict):
        """每个成功回填的文件都更新根目录配置；同实例多文件写出互斥。"""
        config_path = os.path.join(self.output_dir, "uif_config.json")
        with self._jis_lock:
            files = {**self._jis_files, output_path: stats}
            # 保留用户对字体、注入等模块的设置，只更新字符替换部分。
            template = config_path if os.path.isfile(config_path) else self._jis_resource("uif_config.json")
            with open(template, encoding="utf-8") as resource:
                config = json.load(resource)
            substitution = config.setdefault("character_substitution", {})
            previous = self._jis_previous
            if previous is None:
                # 续跑/单文件重建可能没有加载其他已输出脚本，保留上次任务的映射。
                sources = substitution.get("source_characters", "")
                targets = substitution.get("target_characters", "")
                if len(sources) != len(targets):
                    raise RuntimeError(f"UIF 字符替换映射长度不一致：{config_path}")
                previous = {
                    "used": dict(zip(targets, sources)),
                    "remain": set(substitution.get("remain", [])),
                    "repeat": set(substitution.get("repeat", [])),
                }
            used, remain, repeat = {}, set(), set()
            for entry in [previous, *files.values()]:
                used.update(entry["used"])
                remain.update(entry["remain"])
                repeat.update(entry["repeat"])
            if len(set(used.values())) != len(used):
                raise RuntimeError(f"已有 UIF 映射与 JIS 替换字典冲突：{config_path}")
            characters = sorted(used)
            substitution.update({
                "enable": True,
                "source_characters": "".join(used[char] for char in characters),
                "target_characters": "".join(characters),
            })
            for key, values in (("remain", remain), ("repeat", repeat)):
                if values:
                    substitution[key] = sorted(values)
                else:
                    substitution.pop(key, None)

            temporary = None
            try:
                # 先准备配置，再发布脚本；避免配置无法序列化/写入时覆盖已有脚本。
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.output_dir, suffix=".tmp", delete=False,
                ) as resource:
                    temporary = resource.name
                    json.dump(config, resource, ensure_ascii=False, indent=2)
                    resource.write("\n")
                shutil.copyfile(patched_path, output_path)
                os.replace(temporary, config_path)
                self._jis_files = files
                self._jis_previous = previous
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        if stats["remain"]:
            LOGGER.warning(
                f"[{self.pname}] JIS 替换未匹配字符已替换为全角空格："
                + "".join(sorted(stats["remain"])) + f"；详见 {config_path} 中的 remain"
            )
        if stats["repeat"]:
            LOGGER.warning(
                f"[{self.pname}] 原文/译文含有 JIS 替换目标字符，请检查游戏内显示："
                + "".join(sorted(stats["repeat"])) + f"；详见 {config_path} 中的 repeat"
            )

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

    def _load_cached(self, file_path: str, force_reload: bool = False) -> list:
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
                if (not force_reload and isinstance(payload, dict) and payload.get("fingerprint") == fingerprint
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

    def reload_file(self, file_path: str) -> list:
        """忽略旧提取结果，成功后原子更新缓存；失败时保留上次结果。"""
        return self.load_file(file_path, force_reload=True)

    def load_file(self, file_path: str, force_reload: bool = False) -> list:
        """默认复用持久化提取缓存，每次返回独立列表供翻译流程修改。"""
        if not os.path.isfile(file_path):
            raise TypeError(f"文件不存在：{file_path}")
        rows = self._load_cached(file_path, force_reload=force_reload)
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

        jis_stats = self._substitute_jis(rows) if self.jis_substitution else None
        import_args = ["import"]
        patched_encoding = "cp932" if self.jis_substitution else self.patched_encoding
        if patched_encoding and not self._has_extra_option("-p", "--patched-encoding", "-P", "--patched-code-page"):
            import_args += ["-p", patched_encoding]
        import_args += self._common_args(source_path)
        self._preflight_encoding(source_path, rows, import_args)
        with tempfile.TemporaryDirectory(prefix="gt_msgtool_") as tmp_dir:
            trans_json = os.path.join(tmp_dir, "trans.json")
            with open(trans_json, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)

            suffix = os.path.splitext(source_path)[1]
            patched_path = os.path.join(tmp_dir, "patched" + suffix)

            import_args += [source_path, trans_json, patched_path]
            self._run(import_args)

            if not os.path.isfile(patched_path):
                raise RuntimeError(f"msg-tool 未生成回填后的脚本：{source_path}")

            out_dir = os.path.dirname(output_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            if jis_stats is not None:
                self._save_jis_output(patched_path, output_path, jis_stats)
            else:
                shutil.copyfile(patched_path, output_path)

    def gtp_final(self):
        """所有文件翻译完成之后的动作。"""
        self._src_by_out.clear()
        self._jis_files.clear()
