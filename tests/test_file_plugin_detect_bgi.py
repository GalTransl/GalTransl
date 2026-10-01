from pathlib import Path
import tempfile
import unittest

from GalTransl.FilePluginDetect import detect_file_plugin, summarize_detection


class BgiDetectionTests(unittest.TestCase):
    def test_extensionless_bgi_header_routes_to_available_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "mikoto_01"
            script.write_bytes(b"BurikoCompiledScriptVer1.00\0" + b"\0" * 40)
            for plugin in ("file_msgtool_script", "(project_dir)file_msgtool_script"):
                self.assertEqual(detect_file_plugin(str(script), {"._bp": plugin}), plugin)
            self.assertIsNone(detect_file_plugin(str(script), {}))
            detected = {str(script): detect_file_plugin(str(script), {"._bp": "file_msgtool_script"})}
            self.assertEqual(summarize_detection(detected)["suggested"], "file_msgtool_script")

    def test_unknown_extensionless_files_are_not_assumed_to_be_bgi(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "readme"
            for content in (b"", b"plain text", b"BurikoCompiledScriptVer", b"BurikoCompiledScriptVer9.99\0"):
                script.write_bytes(content)
                self.assertIsNone(detect_file_plugin(str(script), {"._bp": "file_msgtool_script"}))
            self.assertIsNone(detect_file_plugin(str(Path(directory) / "missing"), {"._bp": "file_msgtool_script"}))


if __name__ == "__main__":
    unittest.main()
