import unittest

from GalTransl.Agent.runtime import _count_input_file_progress, _input_cache_matchers


class InputCacheMatcherTests(unittest.TestCase):
    """输入文件 → 缓存文件名的换算，须与 LLMTranslate 的命名规则一致。"""

    def test_json_input_single_and_chunked(self) -> None:
        singles, chunk_re = _input_cache_matchers("foo.json")
        self.assertIn("foo.json", singles)
        self.assertTrue(chunk_re.match("foo.json_0.json"))
        self.assertTrue(chunk_re.match("foo.json_1.json.append.jsonl"))

    def test_non_json_input_gets_json_suffix(self) -> None:
        singles, chunk_re = _input_cache_matchers("foo.ks")
        self.assertIn("foo.ks.json", singles)
        self.assertTrue(chunk_re.match("foo.ks_0.json"))
        # 非分块缓存名不应被当成“另一分块”
        self.assertIsNone(chunk_re.match("foo.ks.json"))

    def test_nested_path_separator_replaced(self) -> None:
        singles, _ = _input_cache_matchers("sub/foo.json")
        self.assertIn("sub-}foo.json", singles)


class AgentProjectOverviewFileCountTests(unittest.TestCase):
    """回归：overview 必须给出“已翻译文件数/未翻译文件数”。

    只翻了 1 个文件、但该文件的句数恰好等于缓存句数时，句数会显示 100%，
    不能因此判定项目翻完；文件级计数才是收尾依据。
    """

    def test_only_files_with_real_translation_count_as_translated(self) -> None:
        inputs = ["01.json", "02.json", "03.ks", "sub-01.ks"]
        progress_files = [
            {"filename": "01.json", "translated": 269},  # 单块 json
            {"filename": "02.json", "translated": 0},  # 有缓存但没有译文
            {"filename": "03.ks_0.json", "translated": 3},  # 多块非 json
            {"filename": "03.ks_1.json", "translated": 0},
            {"filename": "sub-01.ks.json", "translated": 5},
            {"filename": "ghost.json", "translated": 9},  # 输入目录外的多余缓存，不计入
        ]

        result = _count_input_file_progress(inputs, progress_files)

        self.assertEqual(result["files_total"], 4)
        self.assertEqual(result["files_translated"], 3)
        self.assertEqual(result["files_untranslated"], 1)

    def test_no_cache_means_all_untranslated(self) -> None:
        result = _count_input_file_progress(["a.json", "b.json"], [])
        self.assertEqual(
            result,
            {"files_total": 2, "files_translated": 0, "files_untranslated": 2},
        )


if __name__ == "__main__":
    unittest.main()
