"""人名表的只读生效视图，供人名工具和项目统计共用。"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from GalTransl.Agent.core import DEFAULT_CONFIG_FILE, _log
from GalTransl.Dictionary import split_dictionary_line

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- GPT 字典用于 name 字段（dictionary.useGPTDictInName）----
# 翻译时（GalTransl/Name.py 的 load_name_table）会拿 GPT 字典里同名的词条覆盖 name 字段，
# 而且这个开关默认就是开的（见 DefaultProjectConfig；缺键也按开启）。也就是说字典收录了的人名，真正生效的
# 是字典译名而不是表里写的——get_name_table 若不体现这一点，模型会去补一整批字典里早就有的
# 名字，或者以为改表就能改掉字典已覆盖的译名。下面这套就是按同一口径把字典译名叠进返回值
# （前端人名页的 overlayGptDictOntoNames 是同一套规则的另一份实现：字典优先于表）。


def _gpt_dict_line(raw: str) -> tuple[str, str]:
    """GPT 字典的一行 →（查找词, 替换词）；不是词条行则返回两个空串。

    共用 GalTransl/Dictionary.py 的列解析：跳过空行/注释行，4 个空格当 Tab，
    兼容 `src->dst #note` 写法，至少两列才算词条。**不做 strip**——字典的命中判定是全等
    比较（CGptDict.get_dst），把空白修掉会让"其实查不到"的行看起来像命中了。
    """
    if not raw or raw.startswith("\n"):
        return "", ""
    if raw.lstrip().startswith(("//", "\\\\")):  # 注释行
        return "", ""
    parts = split_dictionary_line(raw, gpt=True)
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def _gpt_dict_name_map(runner: AgentRunner) -> dict[str, str]:
    """项目 GPT 字典 → {查找词: 替换词}（只读）。

    同一查找词出现多次时取**首次**出现的那条：CGptDict.get_dst 返回的就是第一个 search_word
    全等的词条（字典里"后写的覆盖先写的"在这里不成立）。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
    data = runner._http_get(f"/api/projects/{pid}/dictionary/project?config={cfg}")
    if not isinstance(data, dict):
        return {}
    contents = data.get("dict_contents", {})
    if not isinstance(contents, dict):
        return {}
    out: dict[str, str] = {}
    for key in data.get("gpt_dict_files", []) or []:
        entry = contents.get(str(key))
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("lines", []) or []:
            src, dst = _gpt_dict_line(str(raw))
            if src and dst:
                out.setdefault(src, dst)
    return out


def _use_gpt_dict_in_name(runner: AgentRunner) -> bool:
    """配置里 dictionary.useGPTDictInName 是否开着。

    取值口径与 Name.py 一致（**缺键 = 开启**，与 DefaultProjectConfig 的默认值相同）；只有显式写成
    false 才算关闭。配置读不到则按"没开"处理：这只是附加信息，不值得为一个可选展示把工具弄失败，
    也不该在不确定时给模型补出可能并不生效的译名。
    """
    try:
        pid = runner._project_id()
        cfg = urllib.parse.quote(runner.state.config_file_name or DEFAULT_CONFIG_FILE)
        data = runner._http_get(f"/api/projects/{pid}/config?config={cfg}")
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠ 读项目配置失败，get_name_table 不补 GPT 字典译名：{exc}")
        return False
    config = data.get("config") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        return False
    dictionary = config.get("dictionary")
    if not isinstance(dictionary, dict):
        dictionary = {}
    value = dictionary.get("dictionary.useGPTDictInName", dictionary.get("useGPTDictInName"))
    return True if value is None else bool(value)


def _fill_names_from_gpt_dict(
    names: list[object], gpt_map: dict[str, str]
) -> tuple[list[object], list[str]]:
    """把 GPT 字典里的译名叠到人名行上（只算不写），字典优先于表里已有的译名。

    与 Name.py 的 load_name_table、人名翻译页的 overlayGptDictOntoNames 同一套规则：
    字典收录了的人名一律以字典译名为准；表里原本写了不同译名的，原值放在 table_dst_name，
    让模型看得出"改表不会生效，要改应改字典"。
    返回（叠好的人名行, 表与字典都没有译名的 src_name）。
    """
    out: list[object] = []
    still_empty: list[str] = []
    for item in names:
        if not isinstance(item, dict):
            out.append(item)
            continue
        src = str(item.get("src_name") or item.get("name") or "").strip()
        dst = str(item.get("dst_name") or "")
        mapped = gpt_map.get(src) if src else None
        if not mapped:
            out.append(item)
            if src and not dst.strip():
                still_empty.append(src)
            continue
        # 盖上出处：这行生效的译名来自字典（翻译时字典会覆盖表里的 name 译名）
        row = {**item, "dst_name": mapped, "dst_name_source": "gpt_dict"}
        if dst.strip() and dst != mapped:
            row["table_dst_name"] = dst
        out.append(row)
    return out, still_empty


def _load_effective_name_table(runner: AgentRunner) -> object:
    """读人名替换表；useGPTDictInName 开着时，把 GPT 字典里的译名叠进返回值。

    为什么要叠：翻译时 name 字段会吃 GPT 字典里同名的词条，而且**字典优先于表**。不体现
    这一点，模型会去补一整批"字典里早就写过"的名字。GenDic 完成后，still_empty 才是
    需要检查补漏的候选。

    字典收录了的行（与 Name.py、前端人名页同一套规则）一律显示字典译名，带
    dst_name_source="gpt_dict"；表里原值不同的放在 table_dst_name。无需重复写入人名表；
    要改译名应修改字典。

    **只读**：一个字节都不写回；配置/字典读不到就退回原样返回，绝不因此把工具弄失败。
    """
    pid = runner._project_id()
    data = runner._http_get(f"/api/projects/{pid}/name-table")
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not names:
        return data
    if not _use_gpt_dict_in_name(runner):
        return data
    try:
        gpt_map = _gpt_dict_name_map(runner)
    except Exception as exc:  # noqa: BLE001 - 字典读不到就少补一块，表本身照常返回
        _log(f"  ⚠ 读 GPT 字典失败，get_name_table 返回原始人名表：{exc}")
        return data
    overlaid, still_empty = _fill_names_from_gpt_dict(names, gpt_map)
    return {
        **data,
        "names": overlaid,
        "use_gpt_dict_in_name": True,
        "still_empty": still_empty,
        "note": (
            "dictionary.useGPTDictInName 开着：GPT 字典收录了的 name 一律以字典译名为准，"
            "优先于人名表里写的译名（带 dst_name_source=gpt_dict，翻译时真的会生效；"
            "表里原值不同的见 table_dst_name，改表不会生效，要改应改 GPT 字典）。"
            "still_empty 只列出当前页中表与字典都未提供译名的 name；"
            "字典已覆盖的译名无需重复写入人名表。"
        ),
    }
