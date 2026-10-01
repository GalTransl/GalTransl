import re
from typing import Any, Iterable


def normalize_problem_filter_keys(value) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(key.strip() for key in value if isinstance(key, str) and key.strip()))


def compile_problem_filter_patterns(keys) -> list[re.Pattern]:
    """把过滤项编译成**正则**（正则匹配，不是逐字相等）。

    写错的正则退回按字面匹配：配置可能被手改，一个坏模式不该让整个问题清单/统计挂掉。
    """
    patterns: list[re.Pattern] = []
    for key in normalize_problem_filter_keys(keys):
        try:
            patterns.append(re.compile(key))
        except re.error:
            patterns.append(re.compile(re.escape(key)))
    return patterns


def split_problem_items(problem) -> list[str]:
    """问题文本拆成一个个问题项（英文逗号分隔，与桌面端清单同一套分隔），去空白。

    过滤与计数共用它：两边必须按同一个"项"来切，否则统计出来的数会和实际过滤结果对不上。
    """
    return [item.strip() for item in re.split(r",\s*", str(problem or "")) if item.strip()]


def filter_problem_text(problem, keys) -> str:
    """按「问题项」逐个套正则过滤：命中（re.search）的那一项丢掉。

    problem 是英文逗号分隔的问题项（如「残留日文：おはよう, 缺控制符：<...>」），每个 key
    都是一条正则（如 `缺失.*标点`、`^残留日文：♪`）。原则上只用来过滤小类：`残留日文`、
    `^残留日文：` 这类整类写法会把大类里的真问题一起藏起来。想**按字面**过滤就把特殊字符
    转义（界面/工具里"精确过滤某条"的入口会自动转义）。
    """
    items = split_problem_items(problem)
    if not items:
        return ""
    patterns = compile_problem_filter_patterns(keys)
    if not patterns:
        return ", ".join(items)
    return ", ".join(item for item in items if not any(p.search(item) for p in patterns))


def summarize_problem_filter_hits(problems: Iterable[str], keys) -> dict[str, Any]:
    """一次扫完：每条过滤项各挡住了多少条问题，以及过滤后还剩多少。

    `filters[].problems` 按**问题条目**计（一条命中就算一条），与 filter_problem_text 同一套
    匹配（正则、re.search、坏模式退回字面）。它回答的是「这条过滤项现在还管不管用」：0 说明
    当前缓存里它一条也挡不到，多半是可以删掉的候选。
    `problem_entries` 是有问题的条目总数，`visible_entries` 是过滤后仍会出现在 list_problems
    里的条数（保留任意一个未被命中的问题项就算可见）。
    """
    normalized = normalize_problem_filter_keys(keys)
    patterns = compile_problem_filter_patterns(normalized)
    stats: list[dict[str, Any]] = [{"key": key, "problems": 0} for key in normalized]
    total = 0
    visible = 0
    for problem in problems or []:
        items = split_problem_items(problem)
        if not items:
            continue
        total += 1
        survivors = 0
        for item in items:
            hit = False
            for stat, pattern in zip(stats, patterns):
                if pattern.search(item):
                    stat["problems"] += 1
                    hit = True
            if not hit:
                survivors += 1
        if survivors:
            visible += 1
    return {"filters": stats, "problem_entries": total, "visible_entries": visible}
