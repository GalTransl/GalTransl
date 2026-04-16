import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from GalTransl.Frontend.LLMTranslate import doLLMTranslate


class _FakeProjectConfig:
    def __init__(self, project_dir: str) -> None:
        self._project_dir = project_dir
        self._input_dir = f"{project_dir}\\gt_input"
        self._output_dir = f"{project_dir}\\gt_output"
        self._cache_dir = f"{project_dir}\\transl_cache"

        self.fPlugins = []
        self.tPlugins = []
        self.select_translator = "gpt4"
        self.input_splitter = object()

    def getProjectDir(self):
        return self._project_dir

    def getInputPath(self):
        return self._input_dir

    def getOutputPath(self):
        return self._output_dir

    def getCachePath(self):
        return self._cache_dir

    def getDictCfgSection(self):
        return {
            "preDict": [],
            "postDict": [],
            "gpt.dict": [],
            "defaultDictFolder": "Dict",
        }

    def getKey(self, key, default=None):
        if key == "workersPerProject":
            return 1
        if key == "language":
            return "ja2zh-cn"
        return default


class StartupCacheCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_do_llm_translate_compacts_append_logs_before_scanning_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_config = _FakeProjectConfig(temp_dir)
            compact_mock = AsyncMock(return_value=2)

            with patch("GalTransl.Frontend.LLMTranslate.compact_cache_append_logs", new=compact_mock):
                with patch("GalTransl.Frontend.LLMTranslate.get_file_list", return_value=[]):
                    with self.assertRaises(RuntimeError):
                        await doLLMTranslate(project_config)

            compact_mock.assert_awaited_once_with(project_config.getCachePath())


if __name__ == "__main__":
    unittest.main()
