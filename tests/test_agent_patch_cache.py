import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import AgentToolError, _tool_patch_transl_cache


class _StubRunner:
    """最小 runner：/cache/{file} 读条目，/cache/save 模拟后端重建 problem。

    _model = 本会话 Agent 的模型名（trans_by 自动标记用它），saved 记录写回体。"""

    def __init__(self, entries, *, rebuild=None, model="agent-model"):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._entries = entries
        self._rebuild = rebuild or (lambda ents: ents)
        self._model = model
        self.saved = None

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        return {"entries": [dict(e) for e in self._entries]}

    def _http_post(self, url, body):
        self.saved = [dict(e) for e in body["entries"]]
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


class PatchTransByTests(unittest.TestCase):
    """trans_by 不给模型改：被改过的条目一律自动标成本会话 Agent 的模型名。

    "谁改的"是给人与后续复核看的标记，让模型自己声明（写 manual / agent / 别的模型名）
    会把"翻译引擎翻的"和"Agent 手改的"混成一团。"""

    def test_patched_entry_gets_agent_model_name(self) -> None:
        runner = _StubRunner([{"index": 7, "pre_dst": "旧译文", "trans_by": "gpt-4o"}])

        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 7, "pre_dst": "新译文"}]}
        )

        # 写回文件的那份条目被标成 Agent 的模型名（原来的引擎名被覆盖）
        self.assertEqual(runner.saved[0]["trans_by"], "agent-model")
        self.assertEqual(runner.saved[0]["pre_dst"], "新译文")
        # 只有被改的条目动，结果里也如实报出标的是什么
        self.assertEqual(result["trans_by"], "agent-model")
        # 变更卡只报译文，不把自动标记当成"改动"刷屏
        self.assertEqual([c["path"] for c in result["changes"]], ["#7.pre_dst"])

    def test_manual_trans_by_is_ignored_and_alone_counts_as_no_update(self) -> None:
        runner = _StubRunner([{"index": 1, "pre_dst": "x"}])

        result = _tool_patch_transl_cache(
            runner,
            {
                "filename": "a.json",
                "patches": [
                    {"index": 1, "trans_by": "manual"},  # 只给 trans_by = 没有可更新字段
                    {"index": 1, "pre_dst": "y"},
                ],
            },
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual([s["index"] for s in result["skipped"]], [1])
        self.assertIn("pre_dst / proofread_dst", result["skipped"][0]["reason"])
        self.assertEqual(runner.saved[0]["trans_by"], "agent-model")

    def test_falls_back_to_backend_profile_model(self) -> None:
        """这一回合还没解析后端时（_model 为空），退回 state 里那份配置的模型名。"""
        runner = _StubRunner([{"index": 1, "pre_dst": "x"}], model="")
        runner.state = SimpleNamespace(
            config_file_name="config.yaml",
            backend_profile_data={
                "OpenAI-Compatible": {"tokens": [{"modelName": "deepseek-chat"}]}
            },
            backend_profile_name="Agent 默认",
        )

        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "y"}]}
        )

        self.assertEqual(result["trans_by"], "deepseek-chat")
        self.assertEqual(runner.saved[0]["trans_by"], "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
