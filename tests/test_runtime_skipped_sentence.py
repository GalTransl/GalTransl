import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
import orjson
from GalTransl import CACHE_FOLDERNAME, INPUT_FOLDERNAME
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.tools.project import _tool_get_project_overview
from GalTransl.Cache import save_transCache_to_json, get_transCache_from_json, _build_cache_obj
from GalTransl.CSentense import CSentense
from GalTransl.server import JobRegistry, RuntimeProgressCache, build_handler


def rows():
    result = [CSentense('text', index=1), CSentense('」', index=2), CSentense('next', index=3)]
    for prev, current in zip(result, result[1:]):
        prev.next_tran, current.prev_tran = current, prev
    result[1].post_src = ''
    return result


class SkippedSentenceProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_symbol_counted_and_neighbor_cache_hits_preserved(self):
        with tempfile.TemporaryDirectory() as project:
            cache_path = Path(project)/'transl_cache'/'chapter.json'
            cache_path.parent.mkdir()
            translated = rows()
            for i in (0, 2):
                translated[i].pre_dst = translated[i].post_dst = 'translated'
            await save_transCache_to_json(translated, str(cache_path), post_save=True)
            progress = RuntimeProgressCache().get_progress(project, {'chapter.json': 3}, {'chapter.json': 'chapter.json'})
            self.assertEqual(progress['files'][0]['translated'], 3)
            await save_transCache_to_json(translated, str(cache_path), post_save=False)
            progress = RuntimeProgressCache().get_progress(project, {'chapter.json': 3}, {'chapter.json': 'chapter.json'})
            self.assertEqual(progress['files'][0]['translated'], 3)
            hit, miss = await get_transCache_from_json(rows(), str(cache_path))
            self.assertEqual(len(hit), 3)
            self.assertEqual(miss, [])

    def test_does_not_count_missing_translation_as_skipped(self):
        self.assertIsNone(_build_cache_obj(CSentense('untranslated', index=1)))
        self.assertIsNone(_build_cache_obj(CSentense('', index=2)))
        skipped = rows()[1]
        self.assertTrue(_build_cache_obj(skipped)['translation_skipped'])


class AgentSkippedSentenceProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / CACHE_FOLDERNAME).mkdir()
        (self.project / INPUT_FOLDERNAME).mkdir()
        (self.project / "config.yaml").write_text("common: {}\n", encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.httpd.shutdown)
        self.runner = AgentRunner(
            AgentState(project_dir=str(self.project), config_file_name="config.yaml"),
            port=self.httpd.server_address[1],
        )

    def _progress(self, entries):
        (self.project / INPUT_FOLDERNAME / "chapter.json").write_bytes(orjson.dumps([]))
        (self.project / CACHE_FOLDERNAME / "chapter.json").write_bytes(orjson.dumps(entries))
        return _tool_get_project_overview(self.runner, {"include": ["progress"]})["progress"]

    def test_overview_counts_saved_skipped_line_as_completed(self):
        translated = rows()
        for i in (0, 2):
            translated[i].pre_dst = translated[i].post_dst = "translated"
        progress = self._progress([_build_cache_obj(row) for row in translated])
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["translated"], 3)
        self.assertEqual(progress["files_translated"], 1)
        self.assertEqual(progress["files_untranslated"], 0)
        self.assertEqual(progress["failed"], 0)

    def test_file_with_only_skipped_lines_is_completed(self):
        progress = self._progress([_build_cache_obj(rows()[1])])
        self.assertEqual(progress["translated"], progress["total"])
        self.assertEqual(progress["files_translated"], 1)

    def test_empty_translation_requires_valid_skip_marker(self):
        entries = [
            {"index": 1, "pre_src": "source", "post_src": "", "pre_dst": ""},
            {"index": 2, "post_src": "source", "translation_skipped": True},
            {"index": 3, "post_src": "", "translation_skipped": "true"},
            {"index": 4, "translation_skipped": True},
            {"index": 5, "pre_dst": "translated"},
            {"index": 6, "pre_zh": "legacy translation"},
        ]
        progress = self._progress(entries)
        self.assertEqual(progress["total"], 6)
        self.assertEqual(progress["translated"], 2)
