import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import _tool_list_transl_cache


class _Runner:
    """最小 runner：/cache 返回后端 _list_dir_entries 形状（字段是 entry_count）。"""

    def __init__(self, files):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._files = files

    def _project_id(self):
        return "proj"

    def _http_get(self, _path):
        return {"files": self._files}


class ListTranslCacheEntriesTests(unittest.TestCase):
    """回归：条目数必须读后端的 entry_count，而不是不存在的 entries。"""

    def test_reads_entry_count_from_backend(self) -> None:
        runner = _Runner([
            {"name": "sc_0_pr00.txt.json", "is_file": True, "size": 161091, "entry_count": 269},
        ])
        result = _tool_list_transl_cache(runner, {})
        self.assertEqual(result["cache_files"][0]["entries"], 269)
        self.assertEqual(result["cache_files"][0]["size"], 161091)
        self.assertEqual(result["count"], 1)

    def test_zero_entry_cache_still_reports_zero(self) -> None:
        runner = _Runner([{"name": "empty.json", "is_file": True, "size": 2, "entry_count": 0}])
        result = _tool_list_transl_cache(runner, {})
        # 真实的空缓存要如实报 0，而不是把字段整个丢掉
        self.assertEqual(result["cache_files"][0]["entries"], 0)

    def test_incremental_log_has_no_entries_field(self) -> None:
        runner = _Runner([
            {"name": "a.json", "is_file": True, "size": 10, "entry_count": 5},
            {"name": "a.json.append.jsonl", "is_file": True, "size": 20},  # 后端不统计条目数
        ])
        result = _tool_list_transl_cache(runner, {})
        by_name = {f["name"]: f for f in result["cache_files"]}
        # .append.jsonl 没有条目数，不能伪装成 entries: 0
        self.assertNotIn("entries", by_name["a.json.append.jsonl"])
        self.assertEqual(by_name["a.json.append.jsonl"]["status"], "translating")
        self.assertEqual(result["translating"], 1)


if __name__ == "__main__":
    unittest.main()
