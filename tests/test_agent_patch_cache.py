import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentToolError, _tool_patch_transl_cache


class _StubRunner:
    """最小 runner：/cache/{file} 读条目，/cache/save 模拟后端重建 problem。"""

    def __init__(self, entries, *, rebuild=None):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._entries = entries
        self._rebuild = rebuild or (lambda ents: ents)

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        return {"entries": [dict(e) for e in self._entries]}

    def _http_post(self, url, body):
        return {"success": True, "entries": self._rebuild([dict(e) for e in body["entries"]])}


class PatchTranslCacheReturnShapeTests(unittest.TestCase):
    """修改类工具只返回「改了什么 + 哪里没改成」，不再有多余计数/重复预览。"""

    def test_returns_compact_result_without_redundant_fields(self) -> None:
        runner = _StubRunner([{"index": 33, "pre_dst": "旧译文", "problem": "残留日文"}])
        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 33, "pre_dst": "新译文"}]}
        )

        self.assertEqual(result["filename"], "a.json")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["changes"][0]["path"], "#33.pre_dst")
        self.assertEqual(result["changes"][0]["before"], "旧译文")
        self.assertEqual(result["changes"][0]["after"], "新译文")
        for gone in ("applied", "applied_count", "changed_fields", "saved", "preview"):
            self.assertNotIn(gone, result)

    def test_empty_lists_are_omitted(self) -> None:
        runner = _StubRunner([{"index": 1, "pre_dst": "x"}])
        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "y"}]}
        )
        self.assertNotIn("not_found_indexes", result)
        self.assertNotIn("skipped", result)
        self.assertNotIn("problems", result)

    def test_problems_only_for_changed_entries_still_problematic(self) -> None:
        # 重建后 #33 无问题、#34 仍有问题；未改动的 #35 有问题但不汇报
        def rebuild(ents):
            for e in ents:
                e["problem"] = "仍有问题" if e.get("index") in (34, 35) else ""
            return ents

        runner = _StubRunner(
            [{"index": 33, "pre_dst": "a"}, {"index": 34, "pre_dst": "b"}, {"index": 35, "pre_dst": "c"}],
            rebuild=rebuild,
        )
        result = _tool_patch_transl_cache(
            runner,
            {"filename": "a.json", "patches": [{"index": 33, "pre_dst": "A"}, {"index": 34, "pre_dst": "B"}]},
        )
        self.assertEqual(result["problems"], [{"index": 34, "problem": "仍有问题"}])

    def test_not_found_and_skipped_reported_when_present(self) -> None:
        runner = _StubRunner([{"index": 1, "pre_dst": "x"}])
        result = _tool_patch_transl_cache(
            runner,
            {
                "filename": "a.json",
                "patches": [
                    {"index": 1, "pre_dst": "y"},
                    {"index": 99, "pre_dst": "z"},  # 不存在
                    {"index": 1},  # 存在但无可更新字段
                ],
            },
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["not_found_indexes"], [99])
        self.assertEqual([s["index"] for s in result["skipped"]], [1])
        self.assertTrue(result["skipped"][0]["reason"])

    def test_no_effective_update_raises(self) -> None:
        runner = _StubRunner([{"index": 1, "pre_dst": "x"}])
        with self.assertRaises(AgentToolError):
            _tool_patch_transl_cache(
                runner, {"filename": "a.json", "patches": [{"index": 99, "pre_dst": "z"}]}
            )


if __name__ == "__main__":
    unittest.main()
