"""项目翻译规范：跟着项目走的一份翻译规范文件。

和「全局翻译规范」的分工：
- 全局规范在程序目录的 translation_guidelines/ 下（翻译规范下拉里选的那份），
  是一套放之四海而皆准的通用规则，所有项目共用。
- 项目规范是这一个项目**专用**的补充/覆盖规则，就放在项目目录里（文件名见
  PROJECT_GUIDELINE_FILENAME），跟项目一起走：换机器、拷给别人、提交到仓库都不会丢。

翻译时两份都会拼进翻译器 prompt 的 <translation_guidelines> 段，项目规范在后，
冲突时以项目规范为准（见 combine_guidelines）。

只在翻译器初始化时读一次：写完规范后，**下一次启动翻译**才生效，正在跑的任务不受影响。
"""

from __future__ import annotations

import os
from typing import Any

# 项目目录下的规范文件名。单个文件而不是一个目录：项目规范是一份整体，
# 不需要全局规范那种"多份可选"。
PROJECT_GUIDELINE_FILENAME = "translation_guideline.md"

# 单份规范的字数上限：规范全文每次请求都会进 prompt，无上限的话一次误写
# （比如把整本小说贴进去）会让后面每个请求都爆掉。
MAX_PROJECT_GUIDELINE_CHARS = 200_000

# 拼进 prompt 时给项目规范加的小标题：全局规范在前、项目规范在后，冲突以项目为准
PROJECT_GUIDELINE_HEADING = "## 项目规范（与上面的通用规范冲突时，以本节为准）"

WRITE_MODES = ("overwrite", "append", "replace")


def project_guideline_path(project_dir: str) -> str:
    """项目规范文件的绝对路径。"""
    return os.path.join(os.path.abspath(project_dir), PROJECT_GUIDELINE_FILENAME)


def read_project_guideline(project_dir: str) -> str:
    """读项目规范全文。文件不存在 / 读失败 / 只有空白 → 空串（当作没有）。"""
    if not project_dir:
        return ""
    try:
        with open(project_guideline_path(project_dir), "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    return text if text.strip() else ""


def combine_guidelines(global_text: str, project_text: str) -> str:
    """把全局规范与项目规范拼成一段文本（项目规范在后、带小标题）。"""
    project_text = (project_text or "").strip()
    if not project_text:
        return global_text or ""
    if not (global_text or "").strip():
        return f"{PROJECT_GUIDELINE_HEADING}\n\n{project_text}\n"
    return f"{global_text.rstrip()}\n\n{PROJECT_GUIDELINE_HEADING}\n\n{project_text}\n"


def apply_project_guideline_edit(
    project_dir: str,
    *,
    mode: str,
    content: str = "",
    old_text: str = "",
    new_text: str = "",
) -> dict[str, Any]:
    """按 mode 修改项目规范，返回 {mode, path, existed, created, length}。

    三种模式（Agent 工具与前端编辑页共用这一份实现，行为口径只有一处）：
    - overwrite：整份覆写（文件不存在就创建）
    - append：在当前内容后面增写（文件不存在等同于 overwrite）
    - replace：把 old_text 换成 new_text（局部改，适合只调几条）
      old_text 必须**恰好出现一次**：没找到、或出现多次都直接报错，让它带上
      更多上下文重来——规范文件里"角色名"这种短片段出现多次是常态，
      默默替换掉一处（或全部）比报错更糟。

    参数不合法一律抛 ValueError（调用方转成 400 / 工具报错），不替调用方猜意图。
    """
    if mode not in WRITE_MODES:
        raise ValueError(f"mode 必须是 {'/'.join(WRITE_MODES)} 之一，收到：{mode!r}")
    if not project_dir:
        raise ValueError("project_dir is required")

    path = project_guideline_path(project_dir)
    current = read_project_guideline(project_dir)
    existed = os.path.isfile(path)

    if mode == "overwrite":
        text = content
    elif mode == "append":
        if not content.strip():
            raise ValueError("append 模式需要 content")
        if not current:
            text = content
        else:
            text = f"{current.rstrip()}\n\n{content.strip()}\n"
    else:  # replace
        if not old_text:
            raise ValueError("replace 模式需要 old_text")
        if not existed:
            # 文件都没有，谈不上替换；直接报错比"当成新建"清楚
            raise ValueError(f"项目规范文件还不存在（{PROJECT_GUIDELINE_FILENAME}），请先用 overwrite 创建")
        hits = current.count(old_text)
        if hits == 0:
            raise ValueError("old_text 在项目规范里没有找到，请原样粘贴要改的那段（含前后文）")
        if hits > 1:
            raise ValueError(f"old_text 在项目规范里出现了 {hits} 次，请带上更多前后文只圈定一处")
        text = current.replace(old_text, new_text, 1)

    if len(text) > MAX_PROJECT_GUIDELINE_CHARS:
        raise ValueError(
            f"项目规范过长（{len(text)} 字符，上限 {MAX_PROJECT_GUIDELINE_CHARS}）——"
            "规范全文每次请求都会进 prompt，请精简后再写"
        )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    return {
        "mode": mode,
        "path": path,
        "existed": existed,
        "created": not existed,
        "length": len(text),
        # 提醒调用方（尤其 Agent）：改完不会影响正在跑的任务，下次启动翻译才生效
        "takes_effect": "下次启动翻译时生效",
    }
