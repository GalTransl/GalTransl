"""人名表的只读生效视图，供人名工具和项目统计共用。"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from GalTransl.Agent.core import DEFAULT_CONFIG_FILE, _log
from GalTransl.Dictionary import split_dictionary_line

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- GPT 字典用于 name 字段（dictionary.useGPTDictInName）----
# 翻译时（GalTransl/Name.py 的 load_name_table）会拿 GPT 字典里同名的词条去补 name 字段，
# 而且这个开关默认就是开的（见 DefaultProjectConfig）。也就是说人名表里"译名空着、字典里
# 有"的行其实早已生效——get_name_table 若不体现这一点，模型会去补一整批字典里早就有的名字。
# 下面这套就是按同一口径把字典里的译名补进返回值（前端人名页的 overlayGptDictOntoNames
# 是同一套规则的另一份实现：都只补空译名，不覆盖表里已有的）。


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

    取值口径与 Name.py 一致（**缺键 = 没开**）：配置里没写就意味着翻译时不会拿字典补 name
    字段，这时在 names(action="read") 里补上译名等于骗模型——它会以为这些名字已经有人管了。
    配置读不到也按"没开"处理：这只是附加信息，不值得为一个可选展示把工具弄失败。
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
        return False
    return bool(dictionary.get("dictionary.useGPTDictInName", dictionary.get("useGPTDictInName", False)))


def _fill_names_from_gpt_dict(
    names: list[object], gpt_map: dict[str, str]
) -> tuple[list[object], list[str]]:
    """把 GPT 字典里的译名补到**译名为空**的人名行上（只算不写）。

    与人名翻译页的 overlayGptDictOntoNames 同一套规则：只补空的，不覆盖表里已有的译名——
    表里写下的译名是用户（或 Agent）的决定，字典只回答"这行其实已经有出处了"。
    返回（补好的人名行, 仍然没有译名的 src_name）。
    """
    out: list[object] = []
    still_empty: list[str] = []
    for item in names:
        if not isinstance(item, dict):
            out.append(item)
            continue
        src = str(item.get("src_name") or item.get("name") or "").strip()
        dst = str(item.get("dst_name") or "")
        if dst.strip() or not src:
            out.append(item)
            continue
        mapped = gpt_map.get(src)
        if not mapped:
            out.append(item)
            still_empty.append(src)
            continue
        # 盖上出处：这行的译名不在表里，是字典在翻译时给 name 字段补的
        out.append({**item, "dst_name": mapped, "dst_name_source": "gpt_dict"})
    return out, still_empty


def _load_effective_name_table(runner: AgentRunner) -> object:
    """读人名替换表；useGPTDictInName 开着时，顺带把 GPT 字典里已有的译名补进返回值。

    为什么要补：翻译时 name 字段会吃 GPT 字典里同名的词条，表里译名空着而字典里有人的行
    **其实已经生效**。不体现这一点，模型会去补一整批"字典里早就写过"的名字。补上之后
    GenDic 完成后，still_empty 才是需要检查补漏的候选。

    补的是**空译名**的行（与前端人名页同一套规则）：表里已有的译名不动，字典只作补充说明；
    这些行在返回里带 dst_name_source="gpt_dict"，无需重复写入人名表；要改译名应修改字典。

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
            "dictionary.useGPTDictInName 开着：names 里译名为空、而 GPT 字典收录了的行，"
            "已经按字典的译名补上（带 dst_name_source=gpt_dict，翻译时真的会生效）。"
            "still_empty 只列出当前页中表与字典都未提供译名的 name；"
            "字典已覆盖的译名无需重复写入人名表。"
        ),
    }
