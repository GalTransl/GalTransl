"""Epub 文件插件。

正文按块级元素切分：<p>、<h1>~<h6>、<li>、<td> 等块内连续的行内内容为一句，
<ruby> 的注音（<rt>/<rp>）不送翻译。回写时按源码偏移只替换被翻译的片段，
文档其余部分（声明、样式、空白和换行风格）保持原样。
"""
import codecs
import html as html_lib
import os
import posixpath
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from urllib.parse import unquote

from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
# 块级边界：遇到这些标签就结束当前句子
BLOCK_TAGS = frozenset(
    """address article aside blockquote body caption center dd details dialog div dl dt
    fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr html legend
    li main ol p pre section summary table tbody td tfoot th thead tr ul""".split()
)
# 段落类块：块内的 <br> 是句内换行；其余容器（body、div 等）里的 <br> 视为分句边界
PARAGRAPH_TAGS = frozenset(
    "address caption dd dt figcaption h1 h2 h3 h4 h5 h6 legend li p pre summary td th".split()
)
# 整段跳过、不提取文本的元素
SKIP_TAGS = frozenset("head script style nav svg math template noscript".split())
RUBY_TEXT_TAGS = frozenset(("rt", "rp"))
# 替换时不深入这些元素内部（整段注音结构需要一起替换）
NO_DESCEND_TAGS = frozenset(("ruby", "rb", "rt", "rp", "rtc"))
DOC_MEDIA_TYPES = frozenset(("application/xhtml+xml",))
RULE_SEPARATOR = " => "
_BR = "\x00"
_NEWLINE_SPACE = re.compile(r"[ \t\r\n\f]*[\r\n][ \t\r\n\f]*")
_ENCODING_DECL = re.compile(rb"""(?:encoding|charset)\s*=\s*["']?([A-Za-z0-9._-]+)""")


class _Token:
    __slots__ = ("kind", "tag", "start", "end", "text", "has_id")

    def __init__(self, kind: str, tag: str, start: int, text: str, has_id: bool):
        self.kind = kind  # start / end / void / data / other
        self.tag = tag
        self.start = start
        self.end = start
        self.text = text
        self.has_id = has_id


class _Tokenizer(HTMLParser):
    """把 HTML 拆成首尾相接的标记序列，每个标记带源码偏移。"""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.tokens: list[_Token] = []
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", source)]
        self.feed(source)
        self.close()
        for current, following in zip(self.tokens, self.tokens[1:]):
            current.end = following.start
        if self.tokens:
            self.tokens[-1].end = len(source)

    def _add(self, kind: str, tag: str = "", text: str = "", attrs=()):
        line, column = self.getpos()
        start = self._line_starts[line - 1] + column
        has_id = any(key == "id" for key, _ in attrs)
        self.tokens.append(_Token(kind, tag, start, text, has_id))

    def handle_starttag(self, tag, attrs):
        self._add("void" if tag in VOID_TAGS else "start", tag, attrs=attrs)

    def handle_startendtag(self, tag, attrs):
        self._add("void", tag, attrs=attrs)

    def handle_endtag(self, tag):
        self._add("end", tag)

    def handle_data(self, data):
        self._add("data", text=data)

    def handle_comment(self, data):
        self._add("other")

    handle_decl = handle_pi = unknown_decl = handle_comment


class _Segment:
    """一句正文及其在源码中的替换位置。"""

    __slots__ = ("text", "indent", "start", "end", "balanced", "data_spans", "anchors")

    def __init__(self, text, indent, start, end, balanced, data_spans, anchors):
        self.text = text
        self.indent = indent
        self.start = start
        self.end = end
        self.balanced = balanced
        self.data_spans = data_spans
        self.anchors = anchors


def _is_wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in ("W", "F")


def _collapse_newlines(text: str) -> str:
    """源码里的换行只是排版空白：中日文之间直接去掉，其余折叠为一个空格。"""

    def replace(match):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if not before or not after or _BR in (before, after) or (_is_wide(before) and _is_wide(after)):
            return ""
        return " "

    return _NEWLINE_SPACE.sub(replace, text)


def _is_blank(token: _Token) -> bool:
    return token.kind == "data" and not token.text.strip(" \t\r\n\f")


def _trim(tokens, lo: int, hi: int) -> tuple:
    while lo < hi and _is_blank(tokens[lo]):
        lo += 1
    while hi > lo and _is_blank(tokens[hi - 1]):
        hi -= 1
    return lo, hi


def _balanced(tokens, lo: int, hi: int) -> bool:
    stack = []
    for token in tokens[lo:hi]:
        if token.kind == "start":
            stack.append(token.tag)
        elif token.kind == "end" and token.tag not in VOID_TAGS:
            if not stack or stack[-1] != token.tag:
                return False
            stack.pop()
    return not stack


def _matching_end(tokens, index: int, hi: int):
    tag, depth = tokens[index].tag, 0
    for position in range(index, hi):
        token = tokens[position]
        if token.tag != tag:
            continue
        if token.kind == "start":
            depth += 1
        elif token.kind == "end":
            depth -= 1
            if depth == 0:
                return position
    return None


def _matching_start(tokens, index: int, lo: int):
    tag, depth = tokens[index].tag, 0
    for position in range(index, lo - 1, -1):
        token = tokens[position]
        if token.tag != tag:
            continue
        if token.kind == "end":
            depth += 1
        elif token.kind == "start":
            depth -= 1
            if depth == 0:
                return position
    return None


def _split_runs(tokens) -> list:
    """按块级边界切出行内内容区间 (lo, hi)。"""
    runs, run_start = [], None
    skip_tag, skip_depth = "", 0
    blocks = []  # 当前打开的块级元素

    def close(position):
        if run_start is not None and run_start < position:
            runs.append((run_start, position))

    for index, token in enumerate(tokens):
        if skip_tag:
            if token.tag == skip_tag and token.kind == "start":
                skip_depth += 1
            elif token.tag == skip_tag and token.kind == "end":
                skip_depth -= 1
            elif skip_tag == "head" and token.tag == "body" and token.kind == "start":
                skip_depth = 0  # HTML 允许省略 </head>
            if skip_depth <= 0:
                skip_tag = ""
            continue
        if token.tag in SKIP_TAGS and token.kind == "start":
            close(index)
            run_start = None
            skip_tag, skip_depth = token.tag, 1
        elif token.tag in BLOCK_TAGS or token.tag in SKIP_TAGS:
            close(index)
            run_start = None
            if token.kind == "start":
                if token.tag == "p" and blocks and blocks[-1] == "p":
                    blocks.pop()  # HTML 允许省略 </p>
                blocks.append(token.tag)
            elif token.kind == "end" and token.tag in blocks:
                while blocks.pop() != token.tag:
                    pass
        elif token.tag == "br" and token.kind != "end" and not (blocks and blocks[-1] in PARAGRAPH_TAGS):
            close(index)  # 容器里按 <br> 换行排版的正文，每行一句
            run_start = None
        elif run_start is None:
            run_start = index
    close(len(tokens))
    return runs


def _build_segment(source: str, tokens, lo: int, hi: int):
    parts, annotation = [], False
    for token in tokens[lo:hi]:
        if token.kind == "start" and token.tag in RUBY_TEXT_TAGS:
            annotation = True
        elif token.tag in RUBY_TEXT_TAGS or token.tag in ("ruby", "rb"):
            annotation = False
        elif token.tag == "br" and token.kind != "end":
            parts.append(_BR)
        elif token.kind == "data" and not annotation:
            parts.append(token.text)
    text = _collapse_newlines("".join(parts)).replace(_BR, "\n")
    message = text.strip()
    if not message:
        return None
    leading = text[: len(text) - len(text.lstrip())]
    indent = "".join(char for char in leading if char == "　")

    lo, hi = _trim(tokens, lo, hi)
    # 剥掉跨出本句的首尾标签（如 <br> 分句时包住多行的 <span>），剩余部分通常就能整体替换
    while not _balanced(tokens, lo, hi):
        if tokens[lo].kind == "start" and _matching_end(tokens, lo, hi) is None:
            lo, hi = _trim(tokens, lo + 1, hi)
        elif tokens[hi - 1].kind == "end" and _matching_start(tokens, hi - 1, lo) is None:
            lo, hi = _trim(tokens, lo, hi - 1)
        else:
            break
    balanced = _balanced(tokens, lo, hi)
    # 整句被同一个行内元素包住（如 <a href>、<span class>）时只替换其内部，保留链接和样式
    while balanced and hi - lo >= 2:
        first, last = tokens[lo], tokens[hi - 1]
        if (first.kind != "start" or last.kind != "end" or first.tag != last.tag
                or first.tag in NO_DESCEND_TAGS or not _balanced(tokens, lo + 1, hi - 1)):
            break
        lo, hi = _trim(tokens, lo + 1, hi - 1)

    data_spans = [(token.start, token.end) for token in tokens[lo:hi] if token.kind == "data"]
    anchors, index = [], lo
    while index < hi:  # 没有文字、带 id 的元素（脚注锚点、页码标记）在替换后保留
        token = tokens[index]
        if token.has_id and token.kind == "void" and token.tag not in ("br", "img"):
            anchors.append(source[token.start:token.end])
        elif token.has_id and token.kind == "start":
            close = _matching_end(tokens, index, hi)
            if close is not None and not any(
                t.kind == "data" and t.text.strip() for t in tokens[index + 1:close]
            ):
                anchors.append(source[token.start:tokens[close].end])
                index = close
        index += 1
    return _Segment(message, indent, tokens[lo].start, tokens[hi - 1].end, balanced, data_spans, anchors)


def parse_segments(source: str) -> list:
    tokens = _Tokenizer(source).tokens
    segments = []
    for lo, hi in _split_runs(tokens):
        segment = _build_segment(source, tokens, lo, hi)
        if segment is not None:
            segments.append(segment)
    return segments


def _escape(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return html_lib.escape(text, quote=False).replace("\n", "<br/>")


def _decode(data: bytes):
    """返回 (文本, 编码, 是否带 BOM)；无法解码时返回 None。"""
    try:
        return data.decode("utf-8-sig"), "utf-8", data.startswith(codecs.BOM_UTF8)
    except UnicodeDecodeError:
        pass
    match = _ENCODING_DECL.search(data[:1024])
    if match:
        encoding = match.group(1).decode("ascii")
        try:
            return data.decode(encoding), encoding, False
        except (LookupError, UnicodeDecodeError):
            pass
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def epub_documents(book: zipfile.ZipFile) -> list:
    """按 OPF manifest 顺序列出正文文档（不含导航文档）的 zip 路径。"""
    names = book.namelist()
    try:
        container = ET.fromstring(book.read("META-INF/container.xml"))
        opf_path = next(
            element.get("full-path") for element in container.iter()
            if _local_name(element.tag) == "rootfile" and element.get("full-path")
        )
        opf = ET.fromstring(book.read(opf_path))
    except (KeyError, StopIteration, ET.ParseError):
        return [name for name in names if name.lower().endswith((".xhtml", ".html", ".htm"))]
    existing, base, documents = set(names), posixpath.dirname(opf_path), []
    for element in opf.iter():
        if _local_name(element.tag) != "item" or element.get("media-type") not in DOC_MEDIA_TYPES:
            continue
        if {"nav", "cover"} & set((element.get("properties") or "").split()):
            continue
        path = posixpath.normpath(posixpath.join(base, unquote(element.get("href", ""))))
        if path in existing and path not in documents:
            documents.append(path)
    return documents


class FilePlugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded. 在插件加载时被调用。
        :param plugin_conf: The settings for the plugin. 插件yaml中所有设置的dict。
        :param project_conf: The settings for the project. 项目yaml中common下设置的dict。
        """
        self.pname = plugin_conf["Core"].get("Name", "")
        settings = plugin_conf["Settings"]
        self.是否自动识别名称 = settings.get("是否自动识别名称", False)
        self.名称识别拼接方案 = settings.get("名称识别拼接方案", "{name}\n「{message}」")
        self.名称识别正则表达式 = re.compile(settings.get("名称识别正则表达式", r"^(?P<name>.*?)「(?P<message>.*?)」$"), re.DOTALL)
        self.原文颜色 = settings.get("原文颜色", "#808080")
        self.缩小比例 = settings.get("缩小比例", "0.8")
        self.双语显示 = settings.get("双语显示", True)
        self.译前正则 = self._compile_rules(settings.get("译前正则", []), "译前正则")
        self.译后正则 = self._compile_rules(settings.get("译后正则", []), "译后正则")
        self.project_dir = project_conf["project_dir"]
        self.是否拆分文件以支持单文件多线程 = settings.get("是否拆分文件以支持单文件多线程", False)

        if self.是否拆分文件以支持单文件多线程:
            self.extract_epub()

    def _compile_rules(self, rules, label: str) -> list:
        """每条规则写成「正则 => 替换」，省略替换部分表示删除匹配内容。"""
        compiled = []
        for rule in rules or []:
            if not isinstance(rule, str) or not rule.strip():
                continue
            pattern, _, replacement = rule.partition(RULE_SEPARATOR)
            try:
                compiled.append((re.compile(pattern), replacement))
            except re.error as error:
                LOGGER.warning(f"[{self.pname}] 已忽略无效的{label}「{rule}」：{error}")
        return compiled

    def _apply_rules(self, text: str, rules: list) -> str:
        for pattern, replacement in rules:
            try:
                text = pattern.sub(replacement, text)
            except (re.error, IndexError) as error:
                LOGGER.warning(f"[{self.pname}] 正则「{pattern.pattern}」的替换「{replacement}」无效：{error}")
        return text

    def _parse_document(self, raw: str) -> tuple:
        source = self._apply_rules(raw, self.译前正则)
        return source, parse_segments(source)

    def _entries(self, segments: list, start_index: int, item_id: str = None) -> list:
        entries = []
        for number, segment in enumerate(segments):
            name, message = self.extract_name_message(segment.text)
            entry = {
                "index": start_index + number,
                "name": name,
                "message": message,
                "original_message": segment.text,
                "segment": number,
            }
            if item_id is not None:
                entry["item_id"] = item_id
            entries.append(entry)
        return entries

    def extract_epub(self):
        """
        解压EPUB文件,将所有HTML和XHTML文件复制到 gt_input 文件夹,不保留目录结构。
        记录每个HTML和XHTML文件的原始相对路径信息,以便后续重建目录结构。
        多个epub文件会增加前缀以区分。
        """
        input_dir = os.path.join(self.project_dir, "gt_input")
        epub_files = [file for file in os.listdir(input_dir) if file.endswith(".epub")]

        self.epub_file_info = {}
        used_keys = set()

        for epub_file in epub_files:
            epub_name = os.path.splitext(epub_file)[0]
            epub_path = os.path.join(input_dir, epub_file)
            temp_dir = os.path.join(self.project_dir, "temp", epub_name)
            os.makedirs(temp_dir, exist_ok=True)

            with zipfile.ZipFile(epub_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)

            self.epub_file_info[epub_name] = {}
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith((".html", ".xhtml")):
                        file_path = os.path.join(root, file)
                        rel_path = os.path.relpath(file_path, temp_dir)
                        key = f"{epub_name}_{file}"
                        if key in used_keys:  # 不同目录下的同名章节
                            stem, ext = os.path.splitext(file)
                            number = 2
                            while f"{epub_name}_{stem}_{number}{ext}" in used_keys:
                                number += 1
                            key = f"{epub_name}_{stem}_{number}{ext}"
                        used_keys.add(key)
                        self.epub_file_info[epub_name][key] = rel_path
                        shutil.copy(file_path, os.path.join(input_dir, key))

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load. 文件路径。
        :return: A list of objects with message and name(optional). 返回一个包含message和name(可空)的对象列表。
        """
        if self.是否拆分文件以支持单文件多线程 and file_path.endswith((".html", ".xhtml")):
            return self.read_html_xhtml_to_json(file_path)
        else:
            if not file_path.endswith(".epub"):
                raise TypeError("File type not supported.")
            if file_path.endswith(".epub") and self.是否拆分文件以支持单文件多线程:
                raise TypeError("你开启了文件拆分，跳过epub文件本身。")
            return self.read_epub_to_json(file_path)

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file. 保存文件时被调用。
        :param file_path: The path of the file to save. 保存文件路径。
        :param transl_json: A list of objects same as the return of load_file(). load_file提供的json在翻译message和name后的结果。
        :return: None.
        """
        if self.是否拆分文件以支持单文件多线程:
            self.save_html_xhtml_file(file_path, transl_json)
        else:
            self.save_epub_file(file_path, transl_json)

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作,例如输出提示信息。
        """
        if self.是否拆分文件以支持单文件多线程:
            LOGGER.info(f"[{self.pname}] 正在重建EPUB文件……")
            self.rebuild_epub()

    def read_html_xhtml_to_json(self, file_path: str) -> list:
        """读取拆分出来的单个章节文件。"""
        with open(file_path, "rb") as file:
            decoded = _decode(file.read())
        if decoded is None:
            raise ValueError(f"无法识别 {file_path} 的文本编码")
        _, segments = self._parse_document(decoded[0])
        entries = self._entries(segments, 1)
        # 没有正文（如纯插图页）时保留一个空句，保证保存阶段照常输出该章节
        return entries or [{"index": 1, "name": "", "message": "", "original_message": "", "segment": -1}]

    def read_epub_to_json(self, file_path: str) -> list:
        """
        读取EPUB文件,返回一个包含message和name(可空)的对象列表。
        :param file_path: EPUB文件路径。
        :return: 包含message和name(可空)的对象列表。
        """
        json_list = []
        with zipfile.ZipFile(file_path) as book:
            for document in epub_documents(book):
                decoded = _decode(book.read(document))
                if decoded is None:
                    LOGGER.warning(f"[{self.pname}] 无法识别 {document} 的文本编码，已跳过")
                    continue
                _, segments = self._parse_document(decoded[0])
                json_list.extend(self._entries(segments, len(json_list) + 1, document))
        return json_list

    def extract_name_message(self, text_content: str) -> tuple:
        """
        从文本内容中提取名称和消息。
        :param text_content: 文本内容。
        :return: 包含名称和消息的元组。
        """
        name = ""
        message = text_content
        if self.是否自动识别名称:
            match = self.名称识别正则表达式.search(text_content)
            if match:
                name = match.group("name").strip()
                message = match.group("message").strip()
        return name, message

    def _translated_text(self, entry: dict):
        """返回要写回的译文；未翻译（与原文相同）时返回 None。"""
        original = entry.get("original_message") or ""
        name = str(entry.get("name") or "")
        message = str(entry.get("message") or "")
        if (name, message) == self.extract_name_message(original) or not message:
            return None
        if self.是否自动识别名称 and name:
            return self.名称识别拼接方案.format(name=name, message=message)
        return message

    def _render(self, source: str, segments: list, entries: list, label: str) -> str:
        edits, mismatched = [], 0
        for entry in entries:
            number = entry.get("segment")
            if not isinstance(number, int) or number < 0:
                continue
            if number >= len(segments) or segments[number].text != entry.get("original_message"):
                mismatched += 1
                continue
            text = self._translated_text(entry)
            if text is None:
                continue
            segment = segments[number]
            if segment.indent and not text[:1].isspace():
                text = segment.indent + text
            escaped = _escape(text)
            if segment.balanced and self.双语显示:
                style = html_lib.escape(f"color: {self.原文颜色}; font-size: {self.缩小比例}em;")
                original = source[segment.start:segment.end]
                edits.append((segment.start, segment.end, f'{escaped}<br/><span style="{style}">{original}</span>'))
            elif segment.balanced:
                edits.append((segment.start, segment.end, "".join(segment.anchors) + escaped))
            elif self.双语显示:  # 标签跨块嵌套的少见情况：原文结构不动，译文插在前面
                edits.append((segment.start, segment.start, escaped + "<br/>"))
            else:  # 保留全部标签，译文放进第一个文本节点，其余文本清空
                spans = segment.data_spans
                first = next((span for span in spans if source[span[0]:span[1]].strip()), spans[0])
                edits.extend((start, end, escaped if (start, end) == first else "") for start, end in spans)
        if mismatched:
            LOGGER.warning(f"[{self.pname}] {label} 有 {mismatched} 句与原文对不上（原文或译前正则已改动），这些句子保留原文")
        for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
            source = source[:start] + replacement + source[end:]
        return source

    def _translate_document(self, data: bytes, entries: list, label: str) -> bytes:
        decoded = _decode(data)
        if decoded is None:
            LOGGER.warning(f"[{self.pname}] 无法识别 {label} 的文本编码，保留原文件")
            return data
        raw, encoding, bom = decoded
        source, segments = self._parse_document(raw)
        rendered = self._apply_rules(self._render(source, segments, entries, label), self.译后正则)
        if rendered == raw:
            return data
        return (codecs.BOM_UTF8 if bom else b"") + rendered.encode(encoding, errors="xmlcharrefreplace")

    def save_html_xhtml_file(self, file_path: str, transl_json: list):
        """
        保存翻译后的HTML或XHTML文件。
        :param file_path: 保存文件路径。
        :param transl_json: 包含翻译内容的JSON对象列表。
        """
        input_file_path = file_path.replace("gt_output", "gt_input")
        with open(input_file_path, "rb") as file:
            data = file.read()
        data = self._translate_document(data, transl_json, os.path.basename(file_path))
        with open(file_path, "wb") as file:
            file.write(data)

    def save_epub_file(self, file_path: str, transl_json: list):
        """
        保存翻译后的EPUB文件。
        :param file_path: 保存文件路径。
        :param transl_json: 包含翻译内容的JSON对象列表。
        """
        source_path = file_path.replace("gt_output", "gt_input")
        output_path = file_path.rsplit('.', 1)[0] + '_translated.epub'
        grouped = {}
        for entry in transl_json:
            grouped.setdefault(entry.get("item_id"), []).append(entry)

        temp_path = output_path + ".tmp"
        try:
            with zipfile.ZipFile(source_path) as book, zipfile.ZipFile(temp_path, "w") as output:
                documents = set(epub_documents(book))
                # EPUB 规范要求 mimetype 为第一项且不压缩
                for info in sorted(book.infolist(), key=lambda item: item.filename != "mimetype"):
                    data = book.read(info)
                    if info.filename in documents:
                        data = self._translate_document(data, grouped.get(info.filename, []), info.filename)
                    target = zipfile.ZipInfo(info.filename, info.date_time)
                    target.external_attr = info.external_attr
                    target.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
                    output.writestr(target, data)
            os.replace(temp_path, output_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def rebuild_epub(self):
        """
        重建EPUB文件。
        以原始 temp 目录为底，用 gt_output 中翻译后的章节替换对应文件，最终打包重建EPUB文件。
        """
        temp_dir = os.path.join(self.project_dir, "temp")
        gt_input = os.path.join(self.project_dir, "gt_input")
        gt_output = os.path.join(self.project_dir, "gt_output")

        for epub_name, files in self.epub_file_info.items():
            temp_subdir = os.path.join(temp_dir, epub_name)
            translated = {
                rel_path.replace(os.sep, "/"): os.path.join(gt_output, key) for key, rel_path in files.items()
            }
            members = []
            for root, dirs, names in os.walk(temp_subdir):
                for name in names:
                    full_path = os.path.join(root, name)
                    members.append((os.path.relpath(full_path, temp_subdir).replace(os.sep, "/"), full_path))
            members.sort(key=lambda member: member[0] != "mimetype")

            output_path = os.path.join(gt_output, f"{epub_name}_translated.epub")
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for rel_path, full_path in members:
                    source_path = translated.get(rel_path, full_path)
                    if not os.path.isfile(source_path):
                        LOGGER.warning(f"[{self.pname}] 未找到 {rel_path} 的译文，使用原文")
                        source_path = full_path
                    compress = zipfile.ZIP_STORED if rel_path == "mimetype" else zipfile.ZIP_DEFLATED
                    zipf.write(source_path, rel_path, compress_type=compress)

        shutil.rmtree(temp_dir, ignore_errors=True)
        for file in os.listdir(gt_output):
            if file.endswith((".xhtml", ".html")):
                os.remove(os.path.join(gt_output, file))
        for file in os.listdir(gt_input):
            if file.endswith((".xhtml", ".html")):
                os.remove(os.path.join(gt_input, file))
        LOGGER.info("EPUB文件重建完成，缓存已经清理。")
