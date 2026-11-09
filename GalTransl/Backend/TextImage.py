"""改善拒答「图片输入」方式：把前文和本批输入渲染成 PNG，交给支持视觉的 OpenAI 兼容接口。

文字以图片送达时，接口侧的文本审查看不到原句，模型也更倾向于当作 OCR + 翻译任务处理。
页面尺寸按 OpenAI 的高清图缩放规则取：宽 1024、单页高不超过 1536，缩放后字仍清晰可读；
内容超出一页就分页，按阅读顺序依次附在同一条消息里。
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import List, Optional

from GalTransl import LOGGER

PAGE_WIDTH = 1024
MAX_PAGE_HEIGHT = 1536
PADDING = 28
FONT_SIZE = 22
LINE_HEIGHT = 34
CONTINUATION_INDENT = 2 * FONT_SIZE  # 自动折行的续行缩进，和新的一行区分开
_WRAP_TOKEN = re.compile(r"<br>|.", re.S)  # 折行的最小单位：<br> 整体，其余逐字

# 中日文都要能显示：优先选同时覆盖简繁中文和假名的字体
_WINDOWS_FONTS = (
    "msyh.ttc", "msyh.ttf", "NotoSansSC-VF.ttf", "simsun.ttc",
    "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc",
)
_MAC_FONTS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)
_LINUX_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
)


def _font_candidates(preferred: str = "") -> List[str]:
    candidates = [preferred] if preferred else []
    if sys.platform == "win32":
        font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        candidates += [os.path.join(font_dir, name) for name in _WINDOWS_FONTS]
    elif sys.platform == "darwin":
        candidates += list(_MAC_FONTS)
    else:
        candidates += list(_LINUX_FONTS)
    return candidates


def _fc_match_font() -> str:
    if not shutil.which("fc-match"):
        return ""
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=zh-cn"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return ""
    path = result.stdout.strip()
    return path if path and os.path.isfile(path) else ""


@lru_cache(maxsize=8)
def _load_font(preferred: str = ""):
    from PIL import ImageFont

    for path in _font_candidates(preferred) + [_fc_match_font()]:
        if not path or not os.path.isfile(path):
            continue
        try:
            return ImageFont.truetype(path, FONT_SIZE)
        except Exception:
            continue
    LOGGER.warning("[改善拒答] 未找到可用的中日文字体，图片中的文字可能无法显示；可在配置 gpt.enhance_jailbreak.font 指定字体文件")
    return ImageFont.load_default(size=FONT_SIZE)


def _wrap_line(text: str, font, max_width: float) -> List[str]:
    """按像素宽度逐字折行（中日文没有空格可断）。第一段占满宽度，续行留出缩进。

    换行标记 <br> 当作一个整体，不会被拆到两行。
    """
    if text == "":
        return [""]
    pieces, current, width = [], "", 0.0
    limit = max_width
    for token in _WRAP_TOKEN.findall(text):
        token_width = font.getlength(token)
        if current and width + token_width > limit:
            pieces.append(current)
            current, width = "", 0.0
            limit = max_width - CONTINUATION_INDENT
        current += token
        width += token_width
    pieces.append(current)
    return pieces


def render_text_pages(text: str, font_path: str = "") -> List[bytes]:
    """把多行文本渲染成若干张 PNG（灰度），返回每页的字节。"""
    from PIL import Image, ImageDraw

    font = _load_font(font_path or "")
    text_width = PAGE_WIDTH - 2 * PADDING
    lines_per_page = max(1, (MAX_PAGE_HEIGHT - 2 * PADDING) // LINE_HEIGHT)

    # 每页是若干 (逻辑行序号, 是否续行, 文本)：逻辑行用隔行浅底色分组，折行后也看得出属于同一行；
    # 一个逻辑行尽量不跨页，除非它本身就超过一页
    page_groups, current = [], []
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        parts = _wrap_line(line.replace("\t", "    "), font, text_width)
        group = [(number, index > 0, part) for index, part in enumerate(parts)]
        if current and len(current) + len(group) > lines_per_page:
            page_groups.append(current)
            current = []
        current.extend(group)
        while len(current) > lines_per_page:
            page_groups.append(current[:lines_per_page])
            current = current[lines_per_page:]
    if current:
        page_groups.append(current)

    pages = []
    for page_rows in page_groups:
        height = 2 * PADDING + len(page_rows) * LINE_HEIGHT
        image = Image.new("L", (PAGE_WIDTH, height), 255)
        draw = ImageDraw.Draw(image)
        for offset, (number, continued, part) in enumerate(page_rows):
            top = PADDING + offset * LINE_HEIGHT
            if number % 2 == 1:
                draw.rectangle((0, top, PAGE_WIDTH, top + LINE_HEIGHT), fill=238)
            left = PADDING + (CONTINUATION_INDENT if continued else 0)
            draw.text((left, top + (LINE_HEIGHT - FONT_SIZE) // 2 - 2), part, font=font, fill=0)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pages.append(buffer.getvalue())
    return pages


def render_text_data_urls(text: str, font_path: Optional[str] = "") -> List[str]:
    return [
        "data:image/png;base64," + base64.b64encode(page).decode("ascii")
        for page in render_text_pages(text, font_path or "")
    ]
