import os
import unittest
from types import SimpleNamespace

from GalTransl.CSplitter import EqualPartsSplitter
from GalTransl.Frontend.LLMTranslate import _build_runtime_file_maps, _chunk_progress_slice, _runtime_cross_total


def _chunk(file_path: str, chunk_index: int = 0, total_chunks: int = 1, size: int = 10, cross_num: int = 0, json_list=None):
    # 默认按「前面有 cross_num 句交叉」的中间块摆放
    return SimpleNamespace(
        file_path=file_path,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        start_index=cross_num,
        end_index=cross_num + size,
        chunk_non_cross_size=size,
        cross_num=cross_num,
        json_list=json_list if json_list is not None else [{"message": f"line-{i}"} for i in range(size + cross_num * 2)],
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

    def test_runtime_totals_exclude_empty_message_rows(self) -> None:
        input_dir = os.path.join("proj", "gt_input")
        chunks = [
            _chunk(
                os.path.join(input_dir, "foo.json"),
                size=5,
                json_list=[
                    {"message": "A"},
                    {"message": ""},
                    {"message": "B"},
                    {"message": "   "},
                    {"message": "C"},
                ],
            )
        ]

        file_totals, _ = _build_runtime_file_maps(chunks, input_dir)

        self.assertEqual(file_totals["foo.json"], 3)

    def test_cross_chunks_totals_include_overlap_and_report_cross_count(self) -> None:
        input_dir = os.path.join("proj", "gt_input")
        json_list = [{"message": f"line-{i}"} for i in range(103)]
        json_list[50] = {"message": ""}
        chunks = EqualPartsSplitter(4, 10).split(json_list, os.path.join(input_dir, "foo.json"))
        covered = []
        for chunk in chunks:
            start, end = _chunk_progress_slice(chunk, None, include_cross=False)
            covered.extend(row["message"] for row in chunk.json_list[start:end])
        # 非交叉区：首块从 0 开始，整份文件每句恰好一次
        self.assertEqual(covered, [row["message"] for row in json_list])

        file_totals, _ = _build_runtime_file_maps(chunks, input_dir)
        rows_in_chunks = sum(1 for chunk in chunks for row in chunk.json_list if row["message"])
        # 分母按实际要翻的量：交叉句两边的块各算一次；空行不计
        self.assertEqual(file_totals["foo.json"], rows_in_chunks)
        # 4 块、每侧交叉 10 句共 60 句交叉，其中第 50 行是空行（落在交叉区）不计
        self.assertEqual(_runtime_cross_total(chunks), rows_in_chunks - 102)
        self.assertEqual(_runtime_cross_total(chunks), 59)


if __name__ == "__main__":
    unittest.main()
