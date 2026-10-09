import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from GalTransl import INPUT_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class NestedInputHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nested-input-")
        cls.project = Path(cls.temp.name)
        cls.input = cls.project / INPUT_FOLDERNAME
        (cls.input / "chapter").mkdir(parents=True)
        (cls.project / "config.yaml").write_text(
            "common:\n  language: ja\nplugin:\n  filePlugin: file_galtransl_json\n  textPlugins: []\n", encoding="utf-8")
        for name in ("a.json", "chapter/b.json"):
            (cls.input / name).write_text(json.dumps([{"message": "needle " + name}]), encoding="utf-8")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/{encode_project_dir(str(cls.project))}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as response:
            return json.load(response)

    def search(self, **args):
        request = urllib.request.Request(self.base + "/input/search", method="POST",
                                         data=json.dumps({"query": "needle", **args}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def test_nested_file_is_listed_counted_searched_and_read_using_same_name(self):
        files = self.get("/files?counts=1")["input_files"]
        self.assertEqual([row["name"] for row in files], ["a.json", "chapter/b.json"])
        self.assertEqual([row["sentences"] for row in files], [1, 1])
        found = self.search()
        self.assertEqual(found["total"], 2)
        self.assertEqual([row["filename"] for row in found["results"]], ["a.json", "chapter/b.json"])
        nested = self.search(filename="chapter/b.json")
        self.assertEqual(nested["total"], 1)
        hit = nested["results"][0]
        read = self.get("/input/" + quote(hit["filename"], safe=""))
        self.assertEqual(read["filename"], "chapter/b.json")
        self.assertEqual(read["entries"][0]["pre_src"], hit["src"])
        self.assertEqual(read["entries"][0]["index"], hit["index"])

    def test_traversal_and_absolute_paths_are_rejected(self):
        for name in ("../config.yaml", "..\\config.yaml", str(self.project / "config.yaml")):
            with self.subTest(name=name), self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/input/" + quote(name, safe=""))
            self.assertEqual(ctx.exception.code, 400)

    def test_links_cannot_make_listing_search_or_reads_escape_input(self):
        outside = self.project / "outside.json"
        outside.write_text('[{"message":"needle outside"}]', encoding="utf-8")
        link = self.input / "link.json"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.addCleanup(link.unlink)
        self.assertNotIn("link.json", [row["name"] for row in self.get("/files")["input_files"]])
        self.assertEqual(self.search()["total"], 2)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/input/link.json")
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
