import os
import unittest
from types import SimpleNamespace

from GalTransl.Frontend.LLMTranslate import _build_runtime_file_maps


def _chunk(file_path: str, chunk_index: int = 0, total_chunks: int = 1, size: int = 10):
    return SimpleNamespace(
        file_path=file_path,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        chunk_non_cross_size=size,
    )


class RuntimeFileProgressMappingTests(unittest.TestCase):
    """回归测试：保证 _build_runtime_file_maps 构造的 cache_key 与磁盘实际缓存文件名一致，
    否则「文件进度」Tab 会因为键名不匹配而永远显示 0/total。"""

    def test_json_input_single_chunk_cache_key_matches_disk(self) -> None:
        input_dir = os.path.join("proj", "gt_input")
        chunks = [_chunk(os.path.join(input_dir, "foo.json"))]

        file_totals, cache_map = _build_runtime_file_maps(chunks, input_dir)

        self.assertIn("foo.json", file_totals)
        # 磁盘上的缓存文件名是 foo.json（不会变成 foo.json.json），映射键必须对齐
        self.assertIn("foo.json", cache_map)
        self.assertEqual(cache_map["foo.json"], "foo.json")
        self.assertNotIn("foo.json.json", cache_map)

    def test_non_json_input_cache_key_appends_json_suffix(self) -> None:
        input_dir = os.path.join("proj", "gt_input")
        chunks = [_chunk(os.path.join(input_dir, "foo.ks"))]

        _, cache_map = _build_runtime_file_maps(chunks, input_dir)

        # 非 .json 输入对应的磁盘缓存是 foo.ks.json
        self.assertIn("foo.ks.json", cache_map)
        self.assertEqual(cache_map["foo.ks.json"], "foo.ks")

    def test_json_input_multi_chunk_cache_key_matches_disk(self) -> None:
        input_dir = os.path.join("proj", "gt_input")
        chunks = [
            _chunk(os.path.join(input_dir, "sub", "foo.json"), chunk_index=0, total_chunks=2),
            _chunk(os.path.join(input_dir, "sub", "foo.json"), chunk_index=1, total_chunks=2),
        ]

        file_totals, cache_map = _build_runtime_file_maps(chunks, input_dir)

        self.assertIn("sub/foo.json", file_totals)
        # 多分块：磁盘名形如 sub-}foo.json_0.json（save_transCache_to_json 会补一次 .json）
        self.assertIn("sub-}foo.json_0.json", cache_map)
        self.assertIn("sub-}foo.json_1.json", cache_map)
        self.assertEqual(cache_map["sub-}foo.json_0.json"], "sub/foo.json")


if __name__ == "__main__":
    unittest.main()
