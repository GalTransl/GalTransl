"""插件可选的 SettingsSchema 展示元数据，不改变 Settings 的运行时值。"""
import copy
import math


def normalize_settings_schema(settings, schema):
    if not isinstance(settings, dict) or not isinstance(schema, dict):
        return {}
    result = {}
    for key, default in settings.items():
        source = schema.get(key)
        if not isinstance(source, dict):
            continue
        field = {}
        for name in ("label", "description", "placeholder", "legacy_path"):
            if isinstance(source.get(name), str):
                field[name] = source[name]
        for name in ("advanced", "multiline", "secret"):
            if isinstance(source.get(name), bool):
                field[name] = source[name]
        for name in ("min", "max", "step"):
            value = source.get(name)
            if type(value) in (int, float) and math.isfinite(value):
                field[name] = value
        options = source.get("options")
        option_default = (default[0] if default else "") if isinstance(default, list) else default
        if isinstance(options, list) and type(option_default) in (str, int, float, bool):
            valid = []
            for option in options:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                    continue
                value = option.get("value")
                same_type = type(value) is type(option_default) or (
                    type(value) in (int, float) and type(option_default) in (int, float)
                )
                if same_type and (not isinstance(value, float) or math.isfinite(value)):
                    if not any(item["value"] == value for item in valid):
                        valid.append({"value": value, "label": option["label"]})
            if valid:
                field["options"] = valid
        result[key] = field
    return result


def get_plugin_config_section(project_config, module=None, *, create=False):
    """Treat empty YAML sections as unset; reject malformed non-object values."""
    section = project_config.get("plugin")
    if section is None:
        section = {}
        if create:
            project_config["plugin"] = section
    if not isinstance(section, dict):
        raise ValueError("plugin configuration must be an object")
    if module is None:
        return section
    overrides = section.get(module)
    if overrides is None:
        overrides = {}
        if create:
            section[module] = overrides
    if not isinstance(overrides, dict):
        raise ValueError(f"plugin.{module} configuration must be an object")
    return overrides


def resolve_plugin_settings(plugin_conf, project_config, module):
    """Defaults < declared legacy project paths < explicit plugin overrides."""
    settings = copy.deepcopy(plugin_conf.get("Settings") or {})
    schema = normalize_settings_schema(settings, plugin_conf.get("SettingsSchema"))
    for key, field in schema.items():
        legacy_path = field.get("legacy_path")
        if not legacy_path:
            continue
        node = project_config
        for part in legacy_path.split("."):
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            settings[key] = copy.deepcopy(node)
    settings.update(copy.deepcopy(get_plugin_config_section(project_config, module)))
    return settings
