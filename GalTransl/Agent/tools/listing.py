"""清单类工具的公共入参（grep + limit + order）与挑选逻辑。"""

from __future__ import annotations

import random
from typing import Any

from GalTransl.Agent.models import AgentToolError


# ---- 清单类工具的公共入参（grep + limit + order）----
# read_transl_cache(action=list) / list_input_files 都是一行一个文件的清单：大项目动辄几百上千行，全量
# 倒给模型既费 token 也淹掉重点，只给前 N 行又会让它以为"项目就这些文件"——后面的文件它
# 根本不会去查。所以按 limit（默认 100）截取，**怎么截由 order 决定**（见下面的模式表）；
# 要精确定位某个文件用 grep（文件名子串，大小写不敏感）。
LIST_ITEMS_DEFAULT_LIMIT = 100
LIST_ITEMS_MAX_LIMIT = 500

# order：清单的排列与采样方式。默认 even（均匀采样）——它保证整个范围都有代表，是"先看看
# 项目里都有些什么"的默认姿势；其余几种各有明确用途，都是模型自己点名才会用：
# - even：按文件名排好后均匀采样（含首尾、等距取）；
# - name：按文件名顺序取前 limit 个（挨着看某一批，配合 grep 用）；
# - random：随机采样 limit 个（每次调用可能不同，避免永远只看同一段）；
# - size_desc / size_asc：按文件大小从大到小 / 从小到大取前 limit 个（找大文件优先处理，
#   或先扫小文件）。size 模式的"顺序"本身就是它要表达的东西，所以截断取的是最大/最小的那些。
LIST_ORDER_MODES: tuple[str, ...] = ("even", "name", "random", "size_desc", "size_asc")
LIST_ORDER_DEFAULT = "even"
LIST_ORDER_LABELS: dict[str, str] = {
    "even": "均匀采样",
    "name": "文件名顺序",
    "random": "随机采样",
    "size_desc": "按文件大小从大到小",
    "size_asc": "按文件大小从小到大",
}


def _list_limit(args: dict[str, Any]) -> int:
    """清单工具的 limit：默认 100，夹到 1-500。"""
    raw = args.get("limit", LIST_ITEMS_DEFAULT_LIMIT)
    if raw is None or raw == "":
        return LIST_ITEMS_DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AgentToolError(f"limit 必须是整数（收到 {raw!r}）")
    return max(1, min(value, LIST_ITEMS_MAX_LIMIT))


def _list_grep(args: dict[str, Any]) -> str:
    """清单工具的 grep：文件名过滤词（空串 = 不过滤）。"""
    return str(args.get("grep", "") or "").strip()


def _list_offset(args: dict[str, Any]) -> int:
    value = args.get("offset", 0)
    if type(value) is not int or value < 0:
        raise AgentToolError("offset 必须是非负整数")
    return value


def _list_paging(count: int, returned: int, offset: int, order: str) -> dict[str, Any]:
    # 采样不能靠 offset += returned 完整遍历；只为稳定顺序提供续页位置。
    more = order not in ("even", "random") and offset + returned < count
    return {"order": order, "offset": offset, "has_more": more, **({"next_offset": offset + returned} if more else {})}


def _grep_items(items: list[Any], grep: str) -> list[Any]:
    """按 name 过滤清单（子串、大小写不敏感）。"""
    if not grep:
        return list(items)
    needle = grep.lower()
    return [item for item in items if needle in str(item.get("name", "")).lower()]


def _sample_evenly(items: list[Any], limit: int) -> list[Any]:
    """从长清单里均匀采样 limit 条（含首尾，等距取）。

    与"取前 limit 条"的区别就是这个函数存在的理由：清单按文件名排序，截断会把后半段
    整个藏起来（模型据此以为项目里没有那些文件），采样则每一段都留代表。
    """
    n = len(items)
    if n <= 0:
        return []
    if limit >= n:
        return list(items)
    if limit <= 1:
        return [items[0]]
    return [items[(i * (n - 1)) // (limit - 1)] for i in range(limit)]


def _list_order(args: dict[str, Any]) -> str:
    """清单工具的 order：排列 / 采样方式，默认 even（不当的值直接报错，别静默退回默认）。"""
    raw = str(args.get("order", "") or "").strip().lower()
    if not raw:
        return LIST_ORDER_DEFAULT
    if raw not in LIST_ORDER_MODES:
        raise AgentToolError(f"order 必须是 {'/'.join(LIST_ORDER_MODES)} 之一（收到 {raw!r}）")
    return raw


def _item_size(item: Any) -> tuple[int, str]:
    """大小排序键：（size, name）——size 拿不到当 0，第二关键字用文件名，同尺寸时输出稳定。"""
    size = item.get("size", 0) if isinstance(item, dict) else 0
    try:
        size_i = int(size)
    except (TypeError, ValueError):
        size_i = 0
    name = str(item.get("name", "")) if isinstance(item, dict) else ""
    return (size_i, name)


def _item_name(item: Any) -> str:
    return str(item.get("name", "")) if isinstance(item, dict) else ""


def _select_list_items(items: list[Any], limit: int, order: str, offset: int = 0) -> list[Any]:
    """按 order 从清单里挑出最多 limit 条（各模式唯一的实现，两个清单工具共用）。"""
    if order in ("size_desc", "size_asc"):
        desc = order == "size_desc"
        # 主键是大小；同尺寸一律按文件名升序（连第二关键字一起 reverse 会让输出不可预期）
        def _key(item: Any) -> tuple[int, str]:
            size, name = _item_size(item)
            return (-size, name) if desc else (size, name)

        return sorted(items, key=_key)[offset:offset + limit]
    items = (sorted(items, key=_item_name) if order == "name" else items)[offset:]
    if len(items) <= limit:
        return list(items)  # 用不着截断：原顺序（按文件名）直接给
    if order == "name":
        return list(items[:limit])
    if order == "random":
        # 采样后按文件名排回去：随机的只是"挑中哪些"，清单读起来仍是有序的
        return sorted(random.sample(items, limit), key=_item_name)
    return _sample_evenly(items, limit)  # even


def _list_notes(
    *, matched: int, grep: str, returned: int, limit: int, order: str, unit: str, offset: int = 0
) -> list[str]:
    """过滤 / 截取的说明（各清单工具拼进返回体的 note）。"""
    notes: list[str] = []
    if grep:
        notes.append(f'已按 grep="{grep}" 过滤文件名：命中 {matched} 个{unit}。')
    if order not in ("even", "random"):
        notes.append(f"{LIST_ORDER_LABELS[order]}分页：共 {matched} 个{unit}，本页 {returned} 个（offset={offset}）。")
        if offset + returned < matched:
            notes.append(f"还有更多；保持 grep/order 不变，用 offset={offset + returned} 继续。")
        return notes
    if returned < matched:
        tail = f"要看更多把 limit 调大（当前 {limit}，上限 {LIST_ITEMS_MAX_LIMIT}）；完整遍历用 order=name + limit/offset。"
        if order == "random":
            notes.append(
                f"{matched} 个{unit}超过上限，已**随机采样** {returned} 个"
                "（每次调用挑中的可能不同，这是这个模式的本意）：要多看几批就再调一次，"
                "或用 grep / order=\"size_desc\" 缩小范围；" + tail
            )
        else:
            notes.append(
                f"{matched} 个{unit}超过上限，已从整个清单里**均匀采样** {returned} 个"
                f"（含首尾、等距取，不是前 {returned} 个）："
                "要定位具体文件用 grep 缩小范围；" + tail
            )
    return notes
