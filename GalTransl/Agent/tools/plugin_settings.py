"""发现插件声明的配置，并校验项目覆盖值；不执行插件代码。"""
from __future__ import annotations

import copy
import math
import urllib.parse

from GalTransl.Agent.core import DEFAULT_CONFIG_FILE
from GalTransl.Agent.models import AgentToolError


def load_plugin_catalog(runner):
    data = runner._http_get(f"/api/projects/{runner._project_id()}/plugins")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        raise AgentToolError("无法读取插件声明，请先检查插件列表后再修改插件设置")
    return {p["module"]: p for p in plugins
            if isinstance(p, dict) and isinstance(p.get("module"), str)
            and isinstance(p.get("settings"), dict)}


def catalog_for_updates(runner, updates):
    keys = [str(item.get("key", "")).strip() for item in updates if isinstance(item, dict)]
    if any(key.startswith("plugin.") and key not in ("plugin.filePlugin", "plugin.textPlugins") for key in keys):
        return load_plugin_catalog(runner)
    return {}


def set_plugin_value(config, path, value):
    _, module, key = path.split(".", 2)
    config.setdefault("plugin", {}).setdefault(module, {})[key] = copy.deepcopy(value)


def validate_plugin_value(config, path, value, catalog):
    """返回错误说明；仅允许 Settings 声明过的完整键，不创建任意嵌套路径。"""
    parts = path.split(".", 2)
    if len(parts) != 3:
        return "请使用 get_plugin_settings 返回的完整设置路径，不能覆盖整个插件配置对象"
    _, module, key = parts
    plugin = catalog.get(module)
    if not plugin or key not in plugin["settings"]:
        return "插件未声明此设置；请先调用 get_plugin_settings 确认合法键"
    node = config.get("plugin", {})
    if not isinstance(node, dict) or not isinstance(node.get(module, {}), dict):
        return "现有插件配置不是对象，请先修正项目配置结构"
    default = plugin["settings"][key]
    schema = (plugin.get("settings_schema") or {}).get(key, {})
    if schema.get("secret"):
        return "敏感设置请在桌面端配置，不通过 Agent 修改"
    numeric = type(default) in (int, float)
    if (numeric and (type(value) not in (int, float) or not math.isfinite(value))) or (
        not numeric and type(value) is not type(default)
    ):
        return f"值类型错误，需要 {type(default).__name__}"
    if type(default) is int and type(value) is not int:
        return "值类型错误，需要整数"
    if isinstance(default, list) and default and any(type(v) is not type(default[0]) for v in value):
        return "数组元素类型与插件默认值不一致"
    if numeric:
        if "min" in schema and value < schema["min"]:
            return f"值不能小于 {schema['min']}"
        if "max" in schema and value > schema["max"]:
            return f"值不能大于 {schema['max']}"
    if schema.get("options"):
        allowed = [o["value"] for o in schema["options"]]
        values = value if isinstance(value, list) else [value]
        if any(not any(type(v) is type(a) and v == a for a in allowed) for v in values):
            return f"取值不在插件声明的选项中：{allowed}"
    return None


def _tool_get_plugin_settings(runner, args):
    catalog = load_plugin_catalog(runner)
    requested = str(args.get("plugin_name") or "").strip()
    if requested:
        catalog = {name: p for name, p in catalog.items() if requested in (name, p.get("name"))}
        if not catalog:
            raise AgentToolError(f"未找到插件：{requested}")
    cfg_name = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
    data = runner._http_get(f"/api/projects/{runner._project_id()}/config?config={cfg_name}")
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")
    configured = config.get("plugin", {})
    if not isinstance(configured, dict):
        raise AgentToolError("项目 plugin 配置必须为对象")
    result = []
    for module, plugin in catalog.items():
        overrides = configured.get(module, {})
        if not isinstance(overrides, dict):
            raise AgentToolError(f"plugin.{module} 配置必须为对象")
        fields = []
        for key, default in plugin["settings"].items():
            schema = (plugin.get("settings_schema") or {}).get(key, {})
            secret = schema.get("secret", False)
            fields.append({
                "key": f"plugin.{module}.{key}", "type": type(default).__name__,
                "default": "（隐藏）" if secret else default,
                "value": "（隐藏）" if secret else overrides.get(key, default),
                "overridden": key in overrides, "writable": not secret,
                "schema": schema,
            })
        result.append({"name": module, "display_name": plugin.get("display_name"),
                       "type": plugin.get("type"), "description": plugin.get("description"),
                       "settings": fields})
    return {"plugins": result, "note": "value 是项目覆盖后的生效值；overridden=false 使用默认值。"
            "可用 update_project_config 按完整 key 新增已声明的非敏感设置，保留其他设置。"
            "设置插件参数不会自动选用该插件；文件插件选择见 plugin.filePlugin（auto 可按文件识别）。"}


def encoding_recovery(error):
    if not any(token in error for token in ("脚本回填编码失败", "回填前编码检查未通过", "Failed to encode Shift-JIS", "could not be encoded in Shift-JIS", "JIS 替换字典未覆盖")):
        return None
    return {
        "inspect": {"tool": "get_plugin_settings", "arguments": {"plugin_name": "file_msgtool_script"}},
        "guidance": "先检查当前设置与游戏兼容性，不要盲目重试或重新翻译。游戏必须使用 CP932 时可开启 JIS 替换，"
        "并需 UIF 与生成的 uif_config.json 或对应替换字体；已开启 JIS 时先修正未匹配字符。"
        "仅在游戏支持时改用其他输出编码；原文编码不会改变回填编码。需要改输出编码时关闭 JIS，"
        "并检查 extra_args 的 -p/-P 是否覆盖该值。不要为消除错误而自动把未匹配字符替换为空格。",
        "alternatives": [
            {"condition": "游戏需要 CP932，且能够部署 UIF 或对应字体",
             "tool": "update_project_config", "arguments": {"updates": [
                 {"key": "plugin.file_msgtool_script.jis_substitution", "value": True}]}},
            {"condition": "已确认游戏支持 UTF-8；也可按实际支持情况选择 gb2312",
             "tool": "update_project_config", "arguments": {"updates": [
                 {"key": "plugin.file_msgtool_script.jis_substitution", "value": False},
                 {"key": "plugin.file_msgtool_script.patched_encoding", "value": "utf8"}]}},
        ],
        "rebuild": {"tool": "start_translation", "arguments": {"translator": "rebuildr"}},
        "verify": "确认配置更新成功再重建；wait(job_id=...) 或 get_runtime 确认无编码错误，不能只看缓存已翻译进度。",
    }
