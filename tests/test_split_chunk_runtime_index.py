import unittest

from GalTransl.CSplitter import DictionaryCountSplitter


class SplitChunkRuntimeIndexTests(unittest.TestCase):
    def test_runtime_index_is_global_when_source_has_no_index(self) -> None:
        json_list = [{"message": f"line-{i}"} for i in range(1, 7)]
        splitter = DictionaryCountSplitter(dict_count=3, cross_num=0)

        chunks = splitter.split(json_list, file_path="dummy.json")

        self.assertEqual([getattr(t, "runtime_index", None) for t in chunks[0].trans_list], [1, 2, 3])
        self.assertEqual([getattr(t, "runtime_index", None) for t in chunks[1].trans_list], [4, 5, 6])

    def test_runtime_index_prefers_explicit_index_field(self) -> None:
        json_list = [
            {"message": "a", "index": 10},
            {"message": "b", "index": "11"},
            {"message": "c"},
            {"message": "d"},
        ]
        splitter = DictionaryCountSplitter(dict_count=2, cross_num=0)

        chunks = splitter.split(json_list, file_path="dummy.json")

        self.assertEqual([getattr(t, "runtime_index", None) for t in chunks[0].trans_list], [10, 11])
        self.assertEqual([getattr(t, "runtime_index", None) for t in chunks[1].trans_list], [3, 4])


if __name__ == "__main__":
    unittest.main()
