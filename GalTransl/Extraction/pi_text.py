"""Text contracts ported from the archived Pi 1.4.0 tools (no JS runtime)."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from GalTransl.Agent.models import AgentToolError
from .pi_diff import line_opcodes, unified_patch

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


def format_size(size):
    if size < 1024:
        return f"{size}B"
    value, suffix = (size / 1024, 'KB') if size < 1024**2 else (size / 1024**2, 'MB')
    # JS toFixed rounds exact positive ties up, while Python's formatting uses ties-to-even.
    return f"{Decimal.from_float(value).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}{suffix}"


def truncate(text, *, tail=False):
    lines = text.split("\n") if text else []
    if text.endswith("\n"):
        lines.pop()
    size = len(text.encode("utf-8"))
    result = dict(content=text, truncated=False, truncatedBy=None, totalLines=len(lines), totalBytes=size,
                  outputLines=len(lines), outputBytes=size, lastLinePartial=False, firstLineExceedsLimit=False,
                  maxLines=MAX_LINES, maxBytes=MAX_BYTES)
    if len(lines) <= MAX_LINES and size <= MAX_BYTES:
        return result
    result.update(truncated=True, truncatedBy="lines")
    selected, count = [], 0
    for line in reversed(lines) if tail else lines:
        cost = len(line.encode("utf-8")) + bool(selected)
        if count + cost > MAX_BYTES:
            result["truncatedBy"] = "bytes"
            if not selected:
                if tail:
                    selected.append(line.encode("utf-8")[-MAX_BYTES:].decode("utf-8", errors="ignore"))
                    result["lastLinePartial"] = True
                else:
                    result["firstLineExceedsLimit"] = True
            break
        selected.append(line)
        count += cost
        if len(selected) == MAX_LINES:
            break
    content = "\n".join(reversed(selected) if tail else selected)
    result.update(content=content, outputLines=len(selected), outputBytes=len(content.encode("utf-8")))
    return result


def normalize_lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def fuzzy(text):
    js_whitespace = "\t\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
    text = "\n".join(re.sub(f"[{js_whitespace}]+$", "", line) for line in unicodedata.normalize("NFKC", text).split("\n"))
    text = re.sub("[\u2018-\u201b]", "'", text)
    text = re.sub("[\u201c-\u201f]", '"', text)
    text = re.sub("[\u2010-\u2015\u2212]", "-", text)
    return re.sub("[\u00a0\u2002-\u200a\u202f\u205f\u3000]", " ", text)


def apply_edits(content, edits, path):
    if not edits:
        raise AgentToolError("Edit tool input is invalid. edits must contain at least one replacement.")
    edits = [(normalize_lf(edit["oldText"]), normalize_lf(edit["newText"])) for edit in edits]
    for index, (old, _) in enumerate(edits):
        if not old:
            label = "oldText" if len(edits) == 1 else f"edits[{index}].oldText"
            raise AgentToolError(f"{label} must not be empty in {path}.")
    normalized = fuzzy(content)
    use_fuzzy = any(old not in content and fuzzy(old) in normalized for old, _ in edits)
    base = normalized if use_fuzzy else content
    matches = []
    for index, (old, new) in enumerate(edits):
        needle = old if old in base else fuzzy(old)
        start = base.find(needle)
        if start < 0:
            if len(edits) == 1:
                raise AgentToolError(f"Could not find the exact text in {path}. The old text must match exactly including all whitespace and newlines.")
            raise AgentToolError(f"Could not find edits[{index}] in {path}. The oldText must match exactly including all whitespace and newlines.")
        # Pi checks uniqueness in fuzzy space even when the exact match wins.
        fuzzy_old = fuzzy(old)
        occurrences = fuzzy(base).count(fuzzy_old) if fuzzy_old else max(0, len(fuzzy(base).encode('utf-16-le')) // 2 - 1)
        if occurrences > 1:
            if len(edits) == 1:
                raise AgentToolError(f"Found {occurrences} occurrences of the text in {path}. The text must be unique. Please provide more context to make it unique.")
            raise AgentToolError(f"Found {occurrences} occurrences of edits[{index}] in {path}. Each oldText must be unique. Please provide more context to make it unique.")
        matches.append((start, len(needle), new, index))
    matches.sort(key=lambda item: item[0])
    for before, after in zip(matches, matches[1:]):
        if before[0] + before[1] > after[0]:
            raise AgentToolError(f"edits[{before[3]}] and edits[{after[3]}] overlap in {path}. Merge them into one edit or target disjoint regions.")

    def replace(text, replacements, offset=0):
        for start, length, new, _ in reversed(replacements):
            text = text[:start - offset] + new + text[start - offset + length:]
        return text

    if use_fuzzy:
        # Widen only touched lines; keep original punctuation/spacing elsewhere.
        # splitlines treats Unicode separators as newlines; Pi splits only LF.
        original_lines = re.findall(r"[^\n]*\n|[^\n]+", content)
        base_lines = re.findall(r"[^\n]*\n|[^\n]+", base)
        spans, offset = [], 0
        for line in base_lines:
            spans.append((offset, offset + len(line)))
            offset += len(line)
        if len(original_lines) != len(base_lines):
            raise AgentToolError("Cannot preserve unchanged lines because the base content has a different line count.")
        groups = []
        for match in matches:
            start, length, _, _ = match
            first = next((i for i, (a, b) in enumerate(spans) if a <= start < b), None)
            last = next((i for i, (_, b) in enumerate(spans) if b >= start + length), None)
            if first is None or last is None:
                raise AgentToolError("Replacement range is outside the base content.")
            if groups and first < groups[-1][1]:
                groups[-1][1] = max(groups[-1][1], last + 1)
                groups[-1][2].append(match)
            else:
                groups.append([first, last + 1, [match]])
        pieces, cursor = [], 0
        for first, last, replacements in groups:
            pieces.append("".join(original_lines[cursor:first]))
            a, b = spans[first][0], spans[last - 1][1]
            pieces.append(replace(base[a:b], replacements, a))
            cursor = last
        pieces.append("".join(original_lines[cursor:]))
        updated = "".join(pieces)
    else:
        updated = replace(base, matches)
    if updated == content:
        message = "The replacement produced identical content. This might indicate an issue with special characters or the text not existing as expected." if len(edits) == 1 else "The replacements produced identical content."
        raise AgentToolError(f"No changes made to {path}. {message}")
    return updated


def edit_diff(path, old, new, check_stop=lambda: None):
    """Pi's numbered display and a four-line-context unified patch."""
    before = re.findall(r"[^\n]*\n|[^\n]+", old)
    after = re.findall(r"[^\n]*\n|[^\n]+", new)
    opcodes = line_opcodes(before, after, check_stop)
    width = len(str(max(len(old.split("\n")), len(new.split("\n")))))
    display, first_changed = [], None
    for pos, (tag, a, b, c, d) in enumerate(opcodes):
        if tag != "equal":
            if first_changed is None:
                first_changed = c + 1
            display.extend(f"-{i + 1:>{width}} {before[i].removesuffix(chr(10))}" for i in range(a, b))
            display.extend(f"+{i + 1:>{width}} {after[i].removesuffix(chr(10))}" for i in range(c, d))
            continue
        leading, trailing = pos > 0, pos + 1 < len(opcodes)
        indices = list(range(a, b)) if leading and trailing and b - a <= 8 else (
            list(range(a, min(b, a + 4))) if leading else []) + (
            list(range(max(a, b - 4), b)) if trailing else [])
        cursor = a
        for index in indices:
            if index > cursor:
                display.append(f" {'':>{width}} ...")
            display.append(f" {index + 1:>{width}} {before[index].removesuffix(chr(10))}")
            cursor = index + 1
        if cursor < b and leading:
            display.append(f" {'':>{width}} ...")
    return dict(diff="\n".join(display), patch=unified_patch(path, before, after, opcodes), firstChangedLine=first_changed)
