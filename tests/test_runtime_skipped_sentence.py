import tempfile
import unittest
from pathlib import Path
from GalTransl.Cache import save_transCache_to_json, get_transCache_from_json, _build_cache_obj
from GalTransl.CSentense import CSentense
from GalTransl.server import RuntimeProgressCache


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
