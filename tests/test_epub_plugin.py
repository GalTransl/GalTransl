"""Exercise native EPUB dependencies through real plugin reads and writes."""
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase

from GalTransl.server import JobRegistry, build_handler
from plugins.file_epub_epub.file_epub_epub import FilePlugin
from scripts import smoke_test_release as smoke


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

    def test_release_smoke_parses_epub_txt_and_json_through_real_http_plugins(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                smoke.check_file_plugins(f"http://127.0.0.1:{server.server_port}", Path(tmp))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
