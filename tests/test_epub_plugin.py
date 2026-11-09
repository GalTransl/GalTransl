"""Exercise native EPUB dependencies through real plugin reads and writes."""
import xml.etree.ElementTree as ET
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase

from GalTransl.server import JobRegistry, build_handler
from plugins.file_epub_epub.file_epub_epub import FilePlugin, parse_segments
from scripts import smoke_test_release as smoke

XHTML = "{http://www.w3.org/1999/xhtml}"
CHAPTER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>章タイトル</title><style>p { margin: 0; }</style></head>
<body>
<h1>第一章</h1>
<p>「モテる男は<ruby>辛<rt>つら</rt></ruby>いね」</p>
<p>はい</p>
<p><span class="b">はい</span></p>
<p>　二人の女の子が<span id="page_8"/>火花を散らす。</p>
<p><a href="#n1" id="r1">注1</a></p>
<p>一行目<br/>二行目</p>
<div>直接の文字<p>中の段落</p></div>
<p>A &amp; B</p>
<p><img src="a.png" alt=""/></p>
</body></html>
"""
IMAGE_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>挿絵</title></head><body><p><img src="a.png" alt=""/></p></body></html>
"""
NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目次</title></head>
<body><nav epub:type="toc"><ol><li><a href="chapter.xhtml">第一章</a></li></ol></nav></body></html>
"""
SOURCE_MESSAGES = [
    "第一章", "「モテる男は辛いね」", "はい", "はい", "二人の女の子が火花を散らす。",
    "注1", "一行目\n二行目", "直接の文字", "中の段落", "A & B",
]
TRANSLATIONS = [
    "第1章", "「受欢迎的男人真辛苦啊」", "是", "好的", "两个女孩之间火花四溅。",
    "注释1", "第一行\n第二行", "直接的文字", "里面的段落", "甲 < 乙 & 丙",
]


def write_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
        book.writestr("OEBPS/content.opf", """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier></metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="text/chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/image.xhtml" media-type="application/xhtml+xml"/>
    <item id="img" href="a.png" media-type="image/png"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>""")
        book.writestr("OEBPS/nav.xhtml", NAV)
        book.writestr("OEBPS/text/chapter.xhtml", CHAPTER)
        book.writestr("OEBPS/text/image.xhtml", IMAGE_PAGE)
        book.writestr("OEBPS/a.png", b"\x89PNG fake")


def make_plugin(project: Path, **settings) -> FilePlugin:
    plugin = FilePlugin()
    plugin.gtp_init({"Core": {}, "Settings": settings}, {"project_dir": str(project)})
    return plugin


def translate(entries: list) -> list:
    for entry, translation in zip(entries, TRANSLATIONS, strict=True):
        entry["message"] = translation
    return entries


class EpubPluginTests(TestCase):
    def test_reads_epub_and_writes_translated_text(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "gt_input/smoke.epub"
            output = project / "gt_output/smoke.epub"
            source.parent.mkdir()
            output.parent.mkdir()
            smoke.create_sample_epub(source)
            plugin = FilePlugin()
            plugin.gtp_init({"Core": {}, "Settings": {"双语显示": False}}, {"project_dir": tmp})
            entries = plugin.load_file(str(source))
            self.assertEqual([entry["message"] for entry in entries], [smoke.SAMPLE_TEXT])
            entries[0]["message"] = "你好，世界。"
            plugin.save_file(str(output), entries)
            translated = output.with_name("smoke_translated.epub")
            self.assertEqual([entry["message"] for entry in plugin.load_file(str(translated))], ["你好，世界。"])
            self.assertEqual([entry["message"] for entry in plugin.load_file(str(source))], [smoke.SAMPLE_TEXT])

    def _project(self, tmp: str) -> tuple:
        project = Path(tmp)
        (project / "gt_input").mkdir()
        (project / "gt_output").mkdir()
        write_epub(project / "gt_input/book.epub")
        return project, project / "gt_input/book.epub", project / "gt_output/book.epub"

    def test_extracts_block_text_without_ruby_readings_or_navigation(self):
        with TemporaryDirectory() as tmp:
            project, source, _ = self._project(tmp)
            entries = make_plugin(project).load_file(str(source))
            self.assertEqual([entry["message"] for entry in entries], SOURCE_MESSAGES)
            self.assertEqual([entry["index"] for entry in entries], list(range(1, len(SOURCE_MESSAGES) + 1)))
            self.assertEqual({entry["item_id"] for entry in entries}, {"OEBPS/text/chapter.xhtml"})

    def test_monolingual_output_keeps_markup_and_places_each_translation(self):
        with TemporaryDirectory() as tmp:
            project, source, output = self._project(tmp)
            plugin = make_plugin(project, 双语显示=False)
            plugin.save_file(str(output), translate(plugin.load_file(str(source))))
            translated = output.with_name("book_translated.epub")
            with zipfile.ZipFile(translated) as book, zipfile.ZipFile(source) as original:
                first = book.infolist()[0]
                self.assertEqual((first.filename, first.compress_type), ("mimetype", zipfile.ZIP_STORED))
                self.assertEqual(book.read("OEBPS/text/image.xhtml"), original.read("OEBPS/text/image.xhtml"))
                self.assertEqual(book.read("OEBPS/nav.xhtml"), original.read("OEBPS/nav.xhtml"))
                chapter = book.read("OEBPS/text/chapter.xhtml").decode("utf-8")
            body = ET.fromstring(chapter).find(f"{XHTML}body")
            paragraphs = ["".join(p.itertext()) for p in body.iter(f"{XHTML}p")]
            self.assertEqual(paragraphs[1:4], ["是", "好的", "　两个女孩之间火花四溅。"])
            self.assertIn('<span class="b">好的</span>', chapter)
            self.assertIn('<span id="page_8"/>', chapter)
            self.assertIn('<a href="#n1" id="r1">注释1</a>', chapter)
            self.assertIn("第一行<br/>第二行", chapter)
            self.assertIn("甲 &lt; 乙 &amp; 丙", chapter)
            self.assertIn("<title>章タイトル</title>", chapter)
            self.assertNotIn("<rt>", chapter)
            self.assertEqual([entry["message"] for entry in plugin.load_file(str(translated))], TRANSLATIONS)

    def test_bilingual_output_is_well_formed_without_nested_paragraphs(self):
        with TemporaryDirectory() as tmp:
            project, source, output = self._project(tmp)
            plugin = make_plugin(project, 双语显示=True, 原文颜色="#808080", 缩小比例=0.8)
            plugin.save_file(str(output), translate(plugin.load_file(str(source))))
            with zipfile.ZipFile(output.with_name("book_translated.epub")) as book:
                chapter = book.read("OEBPS/text/chapter.xhtml").decode("utf-8")
            body = ET.fromstring(chapter).find(f"{XHTML}body")
            for paragraph in body.iter(f"{XHTML}p"):
                self.assertEqual([p for p in paragraph.iter(f"{XHTML}p") if p is not paragraph], [])
            self.assertIn('「受欢迎的男人真辛苦啊」<br/><span style="color: #808080; font-size: 0.8em;">'
                          '「モテる男は<ruby>辛<rt>つら</rt></ruby>いね」</span>', chapter)
            self.assertEqual(chapter.count('id="page_8"'), 1)

    def test_untranslated_lines_keep_original_markup(self):
        with TemporaryDirectory() as tmp:
            project, source, output = self._project(tmp)
            plugin = make_plugin(project, 双语显示=True)
            plugin.save_file(str(output), plugin.load_file(str(source)))
            with zipfile.ZipFile(output.with_name("book_translated.epub")) as book:
                self.assertEqual(book.read("OEBPS/text/chapter.xhtml").decode("utf-8"), CHAPTER)

    def test_line_breaks_split_container_text_but_not_paragraphs(self):
        source = """<html><body style="-webkit-writing-mode:vertical-rl;">
    <span style="color:#FF1493;">表題</span>
    <br />
    <br />
    <span style="display:block;text-align:right;">
      <br />作者名<br /></span>
    <div>一行目<br/>二行目</div>
    <p>段落一行目<br/>段落二行目</p>
  </body></html>"""
        texts = [segment.text for segment in parse_segments(source)]
        self.assertEqual(texts, ["表題", "作者名", "一行目", "二行目", "段落一行目\n段落二行目"])
        plugin = make_plugin(Path("unused"), 双语显示=False)
        segments = parse_segments(source)
        entries = [{"segment": n, "original_message": s.text, "name": "", "message": f"译{n}"}
                   for n, s in enumerate(segments)]
        rendered = plugin._render(source, segments, entries, "test")
        self.assertIn('<span style="display:block;text-align:right;">\n      <br />译1<br /></span>', rendered)
        self.assertIn("<div>译2<br/>译3</div>", rendered)

    def test_tags_spanning_several_lines_are_peeled_off_each_line(self):
        source = ('<body><span style="display:block;">\n  <a id="c1" href="a.xhtml">目次一</a>\n  <br />\n'
                  '  <a id="c2" href="b.xhtml">目次二</a>\n</span></body>')
        segments = parse_segments(source)
        self.assertEqual([segment.text for segment in segments], ["目次一", "目次二"])
        self.assertTrue(all(segment.balanced for segment in segments))
        plugin = make_plugin(Path("unused"), 双语显示=True, 原文颜色="#808080", 缩小比例=0.8)
        entries = [{"segment": n, "original_message": s.text, "name": "", "message": f"译{n}"}
                   for n, s in enumerate(segments)]
        rendered = plugin._render(source, segments, entries, "test")
        self.assertIn('<a id="c1" href="a.xhtml">译0<br/><span style="color: #808080; font-size: 0.8em;">目次一</span></a>',
                      rendered)
        self.assertTrue(rendered.startswith('<body><span style="display:block;">'))

    def test_pre_and_post_rules_rewrite_chapter_html(self):
        with TemporaryDirectory() as tmp:
            project, source, output = self._project(tmp)
            plugin = make_plugin(project, 双语显示=False,
                                 译前正则=[r"<ruby>(.*?)<rt>(.*?)</rt></ruby> => \1(\2)", "[invalid"],
                                 译后正则=["第1章 => 第一章"])
            entries = plugin.load_file(str(source))
            self.assertEqual(entries[1]["message"], "「モテる男は辛(つら)いね」")
            plugin.save_file(str(output), translate(entries))
            with zipfile.ZipFile(output.with_name("book_translated.epub")) as book:
                chapter = book.read("OEBPS/text/chapter.xhtml").decode("utf-8")
            self.assertIn("<h1>第一章</h1>", chapter)

    def test_split_mode_round_trip_rebuilds_epub(self):
        with TemporaryDirectory() as tmp:
            project, source, _ = self._project(tmp)
            plugin = make_plugin(project, 双语显示=False, 是否拆分文件以支持单文件多线程=True)
            chapter = project / "gt_input/book_chapter.xhtml"
            image = project / "gt_input/book_image.xhtml"
            self.assertTrue(chapter.is_file() and image.is_file())
            for path in project.glob("gt_input/*.xhtml"):
                entries = plugin.load_file(str(path))
                if path == chapter:
                    translate(entries)
                plugin.save_file(str(project / "gt_output" / path.name), entries)
            plugin.gtp_final()
            translated = project / "gt_output/book_translated.epub"
            with zipfile.ZipFile(translated) as book:
                self.assertEqual(book.infolist()[0].filename, "mimetype")
                self.assertEqual(book.infolist()[0].compress_type, zipfile.ZIP_STORED)
            reader = make_plugin(project / "unused", 双语显示=False)
            self.assertEqual([entry["message"] for entry in reader.load_file(str(translated))], TRANSLATIONS)
            self.assertEqual(list(project.glob("gt_input/*.xhtml")), [])

    def test_release_smoke_parses_epub_txt_and_json_through_real_http_plugins(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                smoke.check_file_plugins(f"http://127.0.0.1:{server.server_port}", Path(tmp))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
