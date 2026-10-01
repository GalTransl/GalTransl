"""文件插件自动识别。

项目配置 plugin.filePlugin 写成 ``auto`` 时，按每个输入文件的类型挑文件插件，
gt_input 里混放不同格式（json + txt + srt……）也能各走各的插件。

识别规则：
1. 插件 yaml 的 ``Core.Extensions`` 声明它能处理的扩展名（第三方插件也能参与）；
2. ``.json`` 有多个插件都认，要看内容：name/message 列表 → GalTransl 标准 json，
   字典 → i18n json。
"""

import os
from typing import Optional

import orjson
import yaml

from GalTransl.RuntimePaths import get_plugins_dir

AUTO_FILE_PLUGIN = "auto"

JSON_GALTRANSL = "file_galtransl_json"
JSON_I18N = "file_i18n_json"
MSGTOOL_SCRIPT = "file_msgtool_script"

# 插件 yaml 没写 Extensions 时的兜底（老版本自带插件）
_BUILTIN_EXTENSIONS: dict[str, list[str]] = {
    "file_plaintext_txt": [".txt"],
    "file_subtitle_srt_lrc_vtt": [".srt", ".lrc", ".vtt"],
    "file_epub_epub": [".epub"],
    "file_translator++_xlsx": [".xlsx"],
}


def is_auto(fname: Optional[str]) -> bool:
    return str(fname or "").strip().lower() == AUTO_FILE_PLUGIN


def _plugin_dirs(project_dir: str = "") -> list[str]:
    dirs = [str(get_plugins_dir())]
    if project_dir:
        dirs.append(os.path.join(project_dir, "plugins"))
    return dirs


def scan_extension_map(project_dir: str = "") -> dict[str, str]:
    """扩展名 → 插件名（项目 plugins 目录里的插件覆盖全局同扩展名的）。"""
    ext_map: dict[str, str] = {}
    for plugin_dir in _plugin_dirs(project_dir):
        if not os.path.isdir(plugin_dir):
            continue
        prefix = "(project_dir)" if plugin_dir != str(get_plugins_dir()) else ""
        for name in sorted(os.listdir(plugin_dir)):
            yaml_path = os.path.join(plugin_dir, name, f"{name}.yaml")
            if not os.path.isfile(yaml_path):
                continue
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    core = (yaml.safe_load(f) or {}).get("Core", {}) or {}
            except Exception:
                continue
            if core.get("Type") != "file":
                continue
            exts = core.get("Extensions") or _BUILTIN_EXTENSIONS.get(name, [])
            if isinstance(exts, str):
                exts = [exts]
            for ext in exts:
                ext = str(ext).lower()
                if not ext.startswith("."):
                    ext = "." + ext
                ext_map[ext] = prefix + name
    return ext_map


def sniff_json_plugin(file_path: str) -> Optional[str]:
    try:
        with open(file_path, "rb") as f:
            data = orjson.loads(f.read())
    except Exception:
        return None
    if isinstance(data, list):
        if not data or all(isinstance(i, dict) and "message" in i for i in data):
            return JSON_GALTRANSL
        return None
    if isinstance(data, dict):
        return JSON_I18N
    return None


def detect_file_plugin(file_path: str, ext_map: Optional[dict[str, str]] = None) -> Optional[str]:
    """识别单个文件应当使用的文件插件，识别不了返回 None。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".json":
        return sniff_json_plugin(file_path)
    if ext_map is None:
        ext_map = scan_extension_map()
    if not ext:
        # BGI 脚本常无扩展名，只接受已知文件头，不能把任意无后缀文件当脚本。
        msgtool = next((name for name in ext_map.values()
                        if name.removeprefix("(project_dir)") == MSGTOOL_SCRIPT), None)
        if msgtool:
            try:
                with open(file_path, "rb") as source:
                    if source.read(28) == b"BurikoCompiledScriptVer1.00\x00":
                        return msgtool
            except OSError:
                pass
    return ext_map.get(ext)


def detect_file_plugins(file_paths: list[str], project_dir: str = "") -> dict[str, Optional[str]]:
    ext_map = scan_extension_map(project_dir)
    return {p: detect_file_plugin(p, ext_map) for p in file_paths}


def summarize_detection(detected: dict[str, Optional[str]]) -> dict:
    """把逐文件识别结果汇总成推荐配置：只有一种格式就用那个插件，多种就 auto。"""
    counts: dict[str, int] = {}
    unknown: list[str] = []
    for path, plugin in detected.items():
        if plugin:
            counts[plugin] = counts.get(plugin, 0) + 1
        else:
            unknown.append(os.path.basename(path))
    if len(counts) == 1:
        suggested = next(iter(counts))
    elif counts:
        suggested = AUTO_FILE_PLUGIN
    else:
        suggested = None
    return {"suggested": suggested, "counts": counts, "unknown": unknown}
