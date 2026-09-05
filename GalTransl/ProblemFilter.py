import re


def normalize_problem_filter_keys(value) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(key.strip() for key in value if isinstance(key, str) and key.strip()))


def filter_problem_text(problem, keys) -> str:
    text = str(problem or "")
    if not keys:
        return text
    # Cache problem messages use the same comma separator as the desktop list.
    return ", ".join(
        part for item in re.split(r",\s*", text)
        if (part := item.strip()) and not any(key in part for key in keys)
    )
