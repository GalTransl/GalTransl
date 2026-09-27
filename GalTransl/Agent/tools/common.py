"""各工具共用的小函数：变更记录、diff、index 区间解析、上下文行标记等。"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _entry_index(entry: Any) -> int:
    """Return a comparable entry index, or -1 for malformed/missing values."""
    if not isinstance(entry, dict):
        return -1
    try:
        return int(entry.get("index", -1))
    except (TypeError, ValueError):
        return -1


def _change(path: str, before: Any, after: Any, kind: str = "replace") -> dict[str, Any]:
    """写入类工具的变更记录：前端据此渲染 diff 风格卡片。

    kind: add（原不存在）/ remove（删后不存在）/ replace（改值）。
    before/after 用 JSON 序列化保持类型可读；超长的文本（如整本字典内容）
    不做 diff，改由工具自己返回统计（行数增删）而非全文。"""
    return {"path": path, "before": before, "after": after, "kind": kind}


def _diff_lines(before_text: str, after_text: str, *, context: int = 0, max_lines: int = 200) -> dict[str, Any]:
    """整文本替换时的逐行 diff（新增行/删除行），给前端渲染行级 diff。

    返回 {"rows": [{"op": "add"|"del", "line": str}], "truncated": bool}——与前端
    extractChangeList 认的 line_diff 结构一致。用最长公共行序列近似（对字典、规范这类
    逐行文本足够准确）；超过 max_lines 时截断并标记 truncated，避免整本小说级 diff 刷屏。"""
    import difflib

    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    rows: list[dict[str, Any]] = []
    truncated = False
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if len(rows) >= max_lines:
            truncated = True
            break
        for line in before_lines[i1:i2]:
            if len(rows) >= max_lines:
                truncated = True
                break
            rows.append({"op": "del", "line": line})
        for line in after_lines[j1:j2]:
            if len(rows) >= max_lines:
                truncated = True
                break
            rows.append({"op": "add", "line": line})
    return {"rows": rows, "truncated": truncated}


def _split_problem_types(problem: str) -> list[str]:
    """问题文本按英文逗号拆项，取每项「类型：详情」的类型前缀去重。

    与桌面端「浏览文本」统计 tab 的归类口径一致（problemFilter.ts）。"""
    types: list[str] = []
    for part in str(problem or "").split(","):
        token = part.strip()
        if not token:
            continue
        type_name = token.split("：", 1)[0].strip()
        if type_name and type_name not in types:
            types.append(type_name)
    return types


def _only_preceding_arg(args: dict[str, Any]) -> bool:
    """only_preceding：带上下文时是否只给上文（**默认 true**，省 token）。

    看「这句话为什么这么翻」通常只需要上文——后文对判断当前这句帮助有限，却要成倍占
    token。模型偶尔把布尔写成字符串（"false"），这里一并认；空串/缺失都按默认 true。
    """
    raw = args.get("only_preceding", True)
    if raw is None:
        return True
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token == "":
            return True
        return token not in ("false", "0", "no", "off")
    return bool(raw)


def _mark_context_row(row: dict[str, Any]) -> dict[str, Any]:
    """把上下文行的 index 标成 "12*"（命中行/问题行保持原样）。

    index 是模型在各工具之间对齐条目的唯一把手，所以"这行是搭着给的上文"就标在它上面：
    带 * 的行不是本页要找的条目，别拿它去 patch、也别当命中数。
    """
    idx = row.get("index")
    if idx is None:
        return row
    return {**row, "index": f"{idx}*"}


def _context_phrase(result: dict[str, Any], subject: str = "条目") -> str:
    """带 context 的工具共用的表头一句话（只给上文 / 两边都给 + 星标的含义）。

    subject 是这个工具"要找的东西"怎么称呼（条目 / 问题 / 命中），三处口径保持一致。
    """
    n = result.get("context")
    if not n:
        return ""
    scope = f"前面 {n} 句" if result.get("only_preceding") else f"前后各 {n} 句"
    kind = "上文" if result.get("only_preceding") else "上下文"
    return f"含{kind}（{subject}{scope}；index 带 * 的是上下文行，不是要找的条目）"


def _parse_index_spec(spec: str) -> set[int]:
    """解析 \"33-40,50-60\" / \"5,9,12\" / \"100-105\" 为 index 集合。

    带上下文的那几个工具会把上下文行的 index 写成 "12*"（见 _mark_context_row）：模型照抄
    过来时按 12 处理——号数是对的，没道理为这个多报一次错。
    """
    result: set[int] = set()
    for part in spec.split(","):
        token = part.strip().replace("*", "")  # 星标（上下文行标记）在 index 里没有别的含义
        if not token:
            continue
        if "-" in token:
            bounds = token.split("-", 1)
            try:
                lo = int(bounds[0])
                hi = int(bounds[1])
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            result.update(range(lo, hi + 1))
        else:
            try:
                result.add(int(token))
            except ValueError:
                continue
    return result


def _dominant_trans_by(rows: list[Any]) -> str:
    """这批条目里出现最多的 trans_by 值（= 这批的"正常情况"，多半就是翻译引擎翻的）。

    逐条重复报它没有信息量，所以调用方把它删掉、改在顶层 majority_trans_by 记一次；少数派
    （Agent 用 patch_transl_cache 改过的、手工改的）才逐条留在 trans_by 上。空值不参与统计，
    一个值都没有时返回空串，调用方据此不做任何过滤。

    按**数据本身**判，而不是跟配置里「翻译任务会用」那份模型名比：项目当初是哪个模型翻的
    只有条目自己知道，用户换过「翻译器默认」之后按配置比会把老条目全当成异常来源显示。
    """
    counts: Counter[str] = Counter(
        str(row.get("trans_by") or "").strip() for row in rows if isinstance(row, dict)
    )
    counts.pop("", None)
    return counts.most_common(1)[0][0] if counts else ""


def _strip_dominant_trans_by(row: dict[str, Any], dominant: str) -> None:
    """就地删掉这一行的 trans_by：空值、或正好是多数派那个值。

    dominant 为空串（这批一个标记都没有）时只删空值——判不出"正常值"就别动，宁可多显示。
    """
    if str(row.get("trans_by") or "").strip() in ("", dominant):
        row.pop("trans_by", None)
