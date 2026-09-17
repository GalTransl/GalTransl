import re


def normalize_problem_filter_keys(value) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(key.strip() for key in value if isinstance(key, str) and key.strip()))


def filter_problem_text(problem, keys) -> str:
    """按「问题项」精准匹配过滤：只丢掉与某个 key **逐字相同**的那一项。

    problem 是英文逗号分隔的问题项（如「残留日文：おはよう, 缺控制符：<...>」），
    key 必须与其中某一项完全一致——不做子串匹配，因而无法用一个词滤掉整个大类
    （写「残留日文」不匹配「残留日文：おはよう」）。
    """
    text = str(problem or "")
    if not keys:
        return text
    wanted = {str(key).strip() for key in keys if str(key).strip()}
    if not wanted:
        return text
    # Cache problem messages use the same comma separator as the desktop list.
    return ", ".join(
        part for item in re.split(r",\s*", text)
        if (part := item.strip()) and part not in wanted
    )
