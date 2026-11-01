"""Compare delivered files with a temporary rebuild from the current cache."""

import asyncio
import copy
import os
from pathlib import Path
import shutil
import tempfile

import orjson

from GalTransl.Cache import get_transCache_from_json
from GalTransl.ConfigHelper import initDictList
from GalTransl.CSerialize import update_json_with_transList
from GalTransl.CSplitter import DictionaryCombiner, DictionaryCountSplitter, EqualPartsSplitter
from GalTransl.Dictionary import CNormalDic, CGptDict
from GalTransl.Frontend.LLMTranslate import preprocess_trans_list, postprocess_trans_list
from GalTransl.GTPlugin import GTextPlugin
from GalTransl.Name import load_name_table
from GalTransl.RuntimePaths import get_plugins_dir
from GalTransl.Utils import get_file_list
from GalTransl.yapsy.PluginManager import PluginManager


def check_cache_output(config, get_file_plugin):
    config.select_translator = "rebuilda"
    config.non_interactive = True
    settings = config.getDictCfgSection()
    for key, attribute, cls in (
        ("preDict", "pre_dic", CNormalDic), ("postDict", "post_dic", CNormalDic), ("gpt.dict", "gpt_dic", CGptDict),
    ):
        dictionary = cls(initDictList(settings.get(key, []), settings.get("defaultDictFolder", ""), config.getProjectDir()))
        if settings.get("sortDict", True):
            dictionary.sort_dic()
        setattr(config, attribute, dictionary)
    plugins = _load_text_plugins(config)
    common = config.getCommonConfigSection()
    mode = common.get("splitFile", "no")
    parts = int(common.get("splitFileNum", -1))
    parts = int(common.get("workersPerProject", -1)) if parts == -1 else parts
    cross = int(common.get("splitFileCrossNum", 0))
    splitter = EqualPartsSplitter(1)
    if mode in ("Num", "Equal"):
        if parts <= 0:
            raise ValueError("splitFileNum 必须大于0，无法按当前分片配置核对缓存")
        splitter = DictionaryCountSplitter(parts, cross) if mode == "Num" else EqualPartsSplitter(parts, cross)
    results = []
    input_files = sorted(get_file_list(config.getInputPath()))
    input_names = {os.path.relpath(filename, config.getInputPath()).replace(os.sep, "/") for filename in input_files}
    for filename in input_files:
        name = os.path.relpath(filename, config.getInputPath()).replace(os.sep, "/")
        try:
            results.append(_check_output_file(config, get_file_plugin, splitter, plugins, filename, name))
        except Exception as exc:
            results.append({"file": name, "status": "uncheckable", "reason": str(exc)[:300]})
    results.extend({"file": name, "status": "uncheckable", "reason": "输出存在，但对应输入文件已缺失，无法从缓存重建核对"}
                   for filename in sorted(get_file_list(config.getOutputPath()))
                   if (name := os.path.relpath(filename, config.getOutputPath()).replace(os.sep, "/")) not in input_names)
    mismatches = [row for row in results if row["status"] == "inconsistent"]
    errors = [row for row in results if row["status"] == "uncheckable"]
    checked = sum(row["status"] in ("consistent", "inconsistent") for row in results)
    details = [row for row in results if row["status"] in ("inconsistent", "uncheckable")]
    status = "inconsistent" if mismatches else "partial" if errors else "consistent" if checked else "not_checked"
    note = "按当前配置、字典、人名表和文本插件，从缓存只读重建后核对最终输出；不修改缓存或项目输出。"
    if mismatches:
        note += '缓存与最终输出不一致，请用 translation(action="start", translator="rebuilda") 刷新缓存与输出。'
    if errors:
        note += "部分文件未能核对，不能据此确认全项目一致；缓存缺失或已过期时，须先补齐或重翻对应文件，rebuilda 无法补译。"
    if not checked:
        note += "没有可核对的缓存与输出。"
    untranslated = sum(row["status"] == "untranslated" for row in results)
    if untranslated:
        note += f"另有{untranslated}个无缓存也无输出的输入文件未参与核对；一致性结论不代表全项目翻译完成。"
    return {
        "status": status,
        "consistent": False if mismatches else None if errors or not checked else True,
        "checked_files": checked,
        "consistent_files": sum(row["status"] == "consistent" for row in results),
        "mismatched_files": len(mismatches),
        "uncheckable_files": len(errors),
        "untranslated_files": untranslated,
        "files": details[:20],
        "omitted_files": max(0, len(details) - 20),
        "rebuild_required": bool(mismatches),
        "note": note,
    }


def _check_output_file(config, get_file_plugin, splitter, plugins, filename, name):
    plugin, _ = get_file_plugin(filename)
    source = plugin.load_file(filename)
    source = source[0] if isinstance(source, tuple) else source
    chunks = splitter.split(source, filename)
    cache_paths = [os.path.join(config.getCachePath(), name.replace("/", "-}") + (
        f"_{chunk.chunk_index}" if len(chunks) > 1 else ""
    )) for chunk in chunks]
    cache_paths = [path if path.endswith(".json") else path + ".json" for path in cache_paths]
    output_path = os.path.join(config.getOutputPath(), name)
    cache_exists = [os.path.isfile(path) or os.path.isfile(path + ".append.jsonl") for path in cache_paths]
    if not any(cache_exists):
        if os.path.isfile(output_path):
            return {"file": name, "status": "uncheckable", "reason": "输出存在，但没有对应缓存；请先恢复或补齐缓存"}
        return {"file": name, "status": "untranslated"}
    if not all(cache_exists):
        return {"file": name, "status": "uncheckable", "reason": "分片缓存不完整；rebuilda 无法补译缺失条目"}
    for chunk, cache_path in zip(chunks, cache_paths):
        for tran in chunk.trans_list:
            tran.source_file_path = filename
            tran.source_file_has_name = any("name" in row or "names" in row for row in source)
        preprocess_trans_list(chunk.trans_list, config, config.pre_dic, plugins)
        _, missing = asyncio.run(get_transCache_from_json(chunk.trans_list, cache_path, eng_type="rebuilda"))
        if missing:
            return {"file": name, "status": "uncheckable", "reason": "缓存缺失或已过期；rebuilda 无法补译",
                    "indexes": [tran.index for tran in missing[:10]]}
        postprocess_trans_list(chunk.trans_list, config, config.post_dic, plugins)
    if not os.path.isfile(output_path):
        return {"file": name, "status": "inconsistent", "reason": "最终输出文件缺失"}
    if Path(name).suffix.lower() in (".xlsx", ".epub"):
        return {"file": name, "status": "uncheckable", "reason": "此二进制格式的重建结果包含非内容元数据，暂不支持可靠比较"}
    name_path = next((os.path.join(config.getProjectDir(), basename) for basename in (
        "name替换表.csv", "name替换表.xlsx",
    ) if os.path.isfile(os.path.join(config.getProjectDir(), basename))), os.path.join(config.getProjectDir(), "name替换表.csv"))
    names = load_name_table(name_path, False, chunks, config)
    sentences, original = DictionaryCombiner.combine(chunks)
    expected = update_json_with_transList(sentences, copy.deepcopy(original), names)
    with tempfile.TemporaryDirectory(prefix="galtransl-output-check-") as temporary:
        target = Path(temporary) / "gt_output" / name
        mirrored_input = Path(temporary) / "gt_input" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        mirrored_input.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(filename, mirrored_input)
        plugin.save_file(str(target), expected)
        rebuilt = target.read_bytes()
    actual = Path(output_path).read_bytes()
    try:
        equal = orjson.loads(rebuilt) == orjson.loads(actual)
    except orjson.JSONDecodeError:
        equal = rebuilt == actual
    return {"file": name, "status": "consistent" if equal else "inconsistent",
            "reason": "" if equal else "最终输出内容与当前缓存的重建结果不同"}


def _load_text_plugins(config):
    manager = PluginManager({"GTextPlugin": GTextPlugin}, [str(get_plugins_dir()), os.path.join(config.getProjectDir(), "plugins")])
    manager.locatePlugins()
    candidates = []
    for name in config.getTextPluginList():
        module = name.removeprefix("(project_dir)")
        root = config.getProjectDir() + "/plugins" if name.startswith("(project_dir)") else str(get_plugins_dir())
        candidate = manager.getPluginCandidateByInfoPath(os.path.join(root, module, module + ".yaml"))
        if candidate is None:
            raise ValueError(f"未找到文本插件：{name}，无法核对输出")
        candidates.append(candidate)
    manager.setPluginCandidates(candidates)
    manager.loadPlugins()
    plugins = manager.getPluginsOfCategory("GTextPlugin")
    if len(plugins) != len(candidates):
        raise ValueError("文本插件加载失败，无法核对输出")
    for plugin in plugins:
        settings = plugin.yaml_dict.setdefault("Settings", {})
        settings.update(config.getPluginConfigSection().get(plugin.yaml_dict["Core"]["Module"]) or {})
        plugin.plugin_object.gtp_init(plugin.yaml_dict, {**config.getCommonConfigSection(), "project_dir": config.getProjectDir()})
    return plugins
