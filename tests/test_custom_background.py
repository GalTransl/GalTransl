import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from GalTransl.CustomBackground import background_directory
from GalTransl.server import JobRegistry, build_handler


class CustomBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/api/custom-background"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "desktop-background"
        patcher = patch("GalTransl.CustomBackground.background_directory", return_value=self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, suffix="", method="GET", data=None, content_type="application/json"):
        request = urllib.request.Request(self.base + suffix, data=data, method=method,
                                         headers={"Content-Type": content_type})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, json.loads(body) if "application/json" in response.headers["Content-Type"] else body

    def upload(self, data=b"image", suffix=""):
        return self.request("/image?name=wallpaper.png" + suffix, "POST", data, "image/png")

    def test_large_original_image_persists_and_reloads_without_base64(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==")
        data = png + bytes(6 * 1024 * 1024)
        self.assertFalse(self.request()[1]["configured"])
        status, saved = self.upload(data)
        self.assertEqual(status, 200)
        self.assertEqual(self.request()[1], saved)
        self.assertEqual(self.request("/image?version=" + saved["imageVersion"]), (200, data))
        self.assertEqual((self.directory / ("image-" + saved["imageVersion"])).read_bytes(), data)
        self.assertLess((self.directory / "settings.json").stat().st_size, 1024)

    def test_opacity_changes_replacement_and_clear_preserve_expected_files(self):
        _, first = self.upload(b"first")
        status, updated = self.request(method="PUT", data=b'{"opacity":70,"surfaceOpacity":60}')
        self.assertEqual(status, 200)
        self.assertEqual(updated["imageVersion"], first["imageVersion"])
        _, second = self.upload(b"second")
        self.assertEqual((second["opacity"], second["surfaceOpacity"]), (70, 60))
        self.assertFalse((self.directory / ("image-" + first["imageVersion"])).exists())
        self.assertEqual(self.request("/image?version=" + first["imageVersion"])[0], 404)
        _, cleared = self.request(method="DELETE")
        self.assertEqual(cleared["imageVersion"], "")
        self.assertTrue(cleared["configured"])
        # Regression: clearing the image used to reset both opacity sliders to defaults.
        self.assertEqual((cleared["opacity"], cleared["surfaceOpacity"]), (70, 60))
        self.assertEqual(self.request()[1], cleared)
        self.assertEqual([item.name for item in self.directory.iterdir()], ["settings.json"])

    def test_only_changed_values_are_written_so_defaults_can_change_later(self):
        # Regression: defaults were stored with the image, so a later default change never reached old users.
        _, saved = self.upload(b"image")
        stored = json.loads((self.directory / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("opacity", stored)
        self.assertNotIn("surfaceOpacity", stored)
        self.request(method="PUT", data=b'{"opacity":60}')
        self.request(method="DELETE")
        self.assertEqual(json.loads((self.directory / "settings.json").read_text(encoding="utf-8")),
                         {"opacity": 60, "storedFormat": 2})
        self.assertTrue(self.request()[1]["configured"])
        self.assertEqual(self.request()[1]["surfaceOpacity"], saved["surfaceOpacity"])

    def test_old_release_defaults_read_as_unset(self):
        # Files from 8.5.3 and earlier held the defaults of that release (35/33) alongside real choices.
        self.directory.mkdir(parents=True)
        (self.directory / "settings.json").write_text('{"opacity": 35, "surfaceOpacity": 60}', encoding="utf-8")
        loaded = self.request()[1]
        self.assertEqual((loaded["opacity"], loaded["surfaceOpacity"]), (45, 60))
        # Once saved with the marker, an explicit 35 is the user's choice and stays.
        self.request(method="PUT", data=b'{"opacity":35}')
        self.assertEqual(self.request()[1]["opacity"], 35)

    def test_failed_replace_or_clear_keeps_previous_image_and_settings(self):
        _, previous = self.upload(b"previous")
        with patch("GalTransl.CustomBackground.os.replace", side_effect=OSError("disk full")):
            self.assertEqual(self.upload(b"replacement")[0], 500)
            self.assertEqual(self.request(method="DELETE")[0], 500)
        self.assertEqual(self.request()[1], previous)
        self.assertEqual(self.request("/image?version=" + previous["imageVersion"]), (200, b"previous"))
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_migration_imports_opacity_and_rejects_invalid_requests(self):
        _, saved = self.upload(suffix="&opacity=48&surfaceOpacity=72")
        self.assertEqual((saved["opacity"], saved["surfaceOpacity"]), (48, 72))
        self.assertEqual(self.request("/image?name=bad", "POST", b"text", "text/html")[0], 400)
        self.assertEqual(self.request(method="PUT", data=b'{"opacity":"invalid"}')[0], 400)
        self.assertEqual(self.request("/image?version=../../other")[0], 404)
        self.assertEqual(self.request()[1], saved)


class BackgroundDirectoryTests(unittest.TestCase):
    def test_program_resources_and_explicit_data_override_on_all_platforms(self):
        for platform in ["win32", "linux", "darwin"]:
            with self.subTest(platform=platform), patch("sys.platform", platform), \
                    patch.dict("os.environ", {"GALTRANSL_DATA_DIR": ""}), \
                    patch("GalTransl.CustomBackground.get_resource_root", return_value=Path("program")):
                self.assertEqual(background_directory(), Path("program/desktop-background"))
                with patch.dict("os.environ", {"GALTRANSL_DATA_DIR": "override"}), \
                        patch("GalTransl.CustomBackground.get_user_data_dir", return_value=Path("override")):
                    self.assertEqual(background_directory(), Path("override/desktop-background"))
