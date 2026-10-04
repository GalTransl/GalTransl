"""Load problem plugins and merge their detection results."""

import copy
import os
import re
import threading
from collections import OrderedDict

from GalTransl import LOGGER
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.CSentense import CTransList
from GalTransl.Dictionary import CGptDict
from GalTransl.GTPlugin import GProblemPlugin
from GalTransl.RuntimePaths import get_plugins_dir
from GalTransl.PluginSettings import resolve_plugin_settings
from GalTransl.yapsy.PluginManager import PluginManager


_type_catalog_cache = OrderedDict()
_type_catalog_lock = threading.Lock()
_TYPE_CATALOG_CACHE_SIZE = 32


def _catalog_fingerprint(roots):
    files = []
    for root in roots:
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
            for name in sorted(names):
                if not name.endswith((".py", ".yaml")):
                    continue
                filename = os.path.join(directory, name)
                try:
                    stat = os.stat(filename)
                except FileNotFoundError:
                    continue
                files.append((filename, stat.st_mtime_ns, stat.st_size))
    return tuple(files)


def _problem_plugin_manager(project_dir):
    roots = [str(get_plugins_dir())]
    if project_dir:
        roots.append(os.path.join(project_dir, "plugins"))
    manager = PluginManager({"GProblemPlugin": GProblemPlugin}, roots)
    manager.locatePlugins()
    return manager


def list_problem_types(project_dir: str = "") -> list[dict]:
    """Discover declared types from all installed problem plugins."""
    roots = [os.path.normcase(os.path.abspath(get_plugins_dir()))]
    if project_dir:
        roots.append(os.path.normcase(os.path.abspath(os.path.join(project_dir, "plugins"))))
    key = tuple(roots)
    # Serialize misses so concurrent HTTP requests don't import duplicate modules.
    with _type_catalog_lock:
        fingerprint = _catalog_fingerprint(roots)
        cached = _type_catalog_cache.get(key)
        if cached is not None and cached[0] == fingerprint:
            _type_catalog_cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        catalog = _discover_problem_types(project_dir)
        _type_catalog_cache[key] = (fingerprint, catalog)
        _type_catalog_cache.move_to_end(key)
        while len(_type_catalog_cache) > _TYPE_CATALOG_CACHE_SIZE:
            _type_catalog_cache.popitem(last=False)
        return copy.deepcopy(catalog)


def _discover_problem_types(project_dir):
    manager = _problem_plugin_manager(project_dir)
    candidates = [
        candidate for candidate in manager.getPluginCandidates()
        if candidate[2].yaml_dict.get("Core", {}).get("Type", "").lower() == "problem"
    ]
    origins = {}
    local_root = os.path.normcase(os.path.abspath(os.path.join(project_dir, "plugins")))
    for info_path, _, info in candidates:
        directory = os.path.dirname(info_path)
        name = os.path.basename(directory)
        local = bool(project_dir) and os.path.normcase(os.path.abspath(os.path.dirname(directory))) == local_root
        origins[id(info)] = f"{'(project_dir)' if local else ''}{name}"
    manager.setPluginCandidates(candidates)
    manager.loadPlugins()
    catalog = {}
    for plugin in manager.getPluginsOfCategory("GProblemPlugin"):
        try:
            types = plugin.plugin_object.get_problem_types()
            if not isinstance(types, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not item["name"].strip() or not isinstance(item.get("description", ""), str)
                or not isinstance(item.get("default_enabled", False), bool)
                for item in types
            ):
                raise TypeError("GProblemPlugin.get_problem_types must return a list of name/description objects")
        except Exception:
            LOGGER.exception("问题插件 %s 读取问题类型失败", plugin.name)
            continue
        for item in types:
            name = item["name"].strip()
            entry = catalog.setdefault(name, {
                "name": name, "description": item.get("description", ""),
                "default_enabled": False, "plugins": []
            })
            entry["default_enabled"] |= item.get("default_enabled", False)
            origin = origins[id(plugin)]
            if origin not in entry["plugins"]:
                entry["plugins"].append(origin)
    return list(catalog.values())


def load_problem_plugins(project_config):
    """Initialize selected problem plugins once per project config instance."""
    loaded = getattr(project_config, "pPlugins", None)
    if loaded is not None:
        return loaded
    project_dir = getattr(project_config, "getProjectDir", lambda: "")()
    names = getattr(project_config, "getProblemPluginList", lambda: ["problem_common"])()
    if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("plugin.problemPlugins must be a list of non-empty plugin names")
    if not names:
        project_config.pPlugins = []
        return []
    manager = _problem_plugin_manager(project_dir)
    candidates = []
    for name in names:
        local = name.startswith("(project_dir)")
        module = name.removeprefix("(project_dir)")
        root = os.path.join(project_dir, "plugins") if local else str(get_plugins_dir())
        info_path = os.path.join(root, module, f"{module}.yaml")
        candidate = manager.getPluginCandidateByInfoPath(info_path)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        elif not candidate:
            LOGGER.warning("未找到问题插件: %s，跳过该插件", name)
    manager.setPluginCandidates(candidates)
    manager.loadPlugins()
    project_conf = copy.deepcopy(getattr(project_config, "getCommonConfigSection", lambda: {})())
    project_conf["project_dir"] = project_dir
    config_data = getattr(project_config, "getProjectConfig", lambda: {
        "plugin": getattr(project_config, "getPluginConfigSection", lambda: {})()
    })()
    plugins = []
    for plugin in manager.getPluginsOfCategory("GProblemPlugin"):
        try:
            plugin_conf = copy.deepcopy(plugin.yaml_dict)
            module = plugin_conf["Core"]["Module"]
            plugin_conf["Settings"] = resolve_plugin_settings(plugin_conf, config_data, module)
            LOGGER.info("加载问题插件: %s", plugin.name)
            plugin.plugin_object.gtp_init(plugin_conf, copy.deepcopy(project_conf))
        except Exception:
            LOGGER.exception("问题插件 %s 初始化失败", plugin.name)
            continue
        plugins.append(plugin)
    project_config.pPlugins = plugins
    return plugins


def finalize_problem_plugins(project_config):
    """Finish a run even if an individual plugin's final hook fails."""
    for plugin in getattr(project_config, "pPlugins", None) or []:
        try:
            plugin.plugin_object.gtp_final()
        except Exception:
            LOGGER.exception("问题插件 %s 结束处理失败", plugin.name)
    project_config.pPlugins = None


def find_problems(
    trans_list: CTransList,
    projectConfig: CProjectConfig,
    gpt_dict: CGptDict = None,
) -> None:
    plugins = load_problem_plugins(projectConfig)
    for tran in trans_list:
        if getattr(tran, "skip_check", False):
            tran.problem = ""
            continue
        if not tran.pre_dst:
            continue
        existing = {p.strip() for p in re.split(r",\s*", tran.problem or "") if p.strip()}
        additions = []
        for plugin in plugins:
            try:
                messages = plugin.plugin_object.check(tran, projectConfig, gpt_dict)
                if not isinstance(messages, list) or any(not isinstance(item, str) for item in messages):
                    raise TypeError("GProblemPlugin.check must return list[str]")
                for item in messages:
                    item = item.strip()
                    if item and item not in existing:
                        existing.add(item)
                        additions.append(item)
            except Exception:
                LOGGER.exception("问题插件 %s 执行失败 (index=%s)", plugin.name, tran.index)
        if additions:
            extra = ", ".join(additions)
            tran.problem = f"{tran.problem}, {extra}" if tran.problem else extra
