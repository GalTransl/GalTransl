import unittest
import urllib.parse
from types import SimpleNamespace

from GalTransl.Agent.runtime import (
    SUBAGENT_PATCHABLE_FIELDS,
    AgentToolError,
    _preview_cache_patch,
    _render_tool_result_table,
    _tool_patch_transl_cache,
)


class _StubRunner:
    """最小 runner：/cache/{file} 读条目，/cache/save 模拟后端重建 problem。

    entries 有两种给法：
    - dict（文件名 → 条目）：跨文件用例，每个文件读到自己那份；
    - list：所有文件名都返回这一份（老用例不必改）。
    _model = 本会话 Agent 的模型名（trans_by 自动标记用它），saved 按文件名记录写回体。"""

    def __init__(self, entries, *, rebuild=None, model="agent-model"):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._by_file = (
            {name: [dict(e) for e in rows] for name, rows in entries.items()}
            if isinstance(entries, dict)
            else None
        )
        self._entries = None if self._by_file is not None else [dict(e) for e in entries]
        self._rebuild = rebuild or (lambda ents: ents)
        self._model = model
        self.saved: dict[str, list] = {}
        self.reads: list[str] = []

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        self.reads.append(name)
        rows = self._by_file.get(name, []) if self._by_file is not None else self._entries
        return {"entries": [dict(e) for e in rows or []]}

    def _http_post(self, url, body):
        name = str(body["filename"])
        self.saved[name] = [dict(e) for e in body["entries"]]
        return {"success": True, "entries": self._rebuild([dict(e) for e in body["entries"]])}


class PatchTranslCacheReturnShapeTests(unittest.TestCase):
    """修改类工具只返回「改了什么 + 哪里没改成」，不再有多余计数/重复预览。

    结果按文件分组（files[]）：一次调用可以跨多个文件，改了什么、哪条没落地都挂在对应文件上。
    """

    def test_returns_compact_result_without_redundant_fields(self) -> None:
        runner = _StubRunner([{"index": 33, "pre_dst": "旧译文", "problem": "残留日文"}])
        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 33, "pre_dst": "新译文"}]}
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["files"][0]["filename"], "a.json")
        change = result["files"][0]["changes"][0]
        self.assertEqual(change["path"], "#33.pre_dst")
        self.assertEqual(change["before"], "旧译文")
        self.assertEqual(change["after"], "新译文")
        # 顶层 changes 是给前端变更卡用的汇总（见 extractChangeList），与分组里那份一致
        self.assertEqual(result["changes"], result["files"][0]["changes"])
        for gone in ("applied", "applied_count", "changed_fields", "saved", "preview"):
            self.assertNotIn(gone, result)

    def test_empty_lists_are_omitted(self) -> None:
        runner = _StubRunner([{"index": 1, "pre_dst": "x"}])
        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "y"}]}
        )
        self.assertNotIn("not_found", result["files"][0])
        self.assertNotIn("skipped", result["files"][0])
        self.assertNotIn("problems", result["files"][0])

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
        self.assertEqual(
            result["files"][0]["problems"],
            [{"file": "a.json", "index": 34, "problem": "仍有问题"}],
        )

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
        self.assertEqual(result["files"][0]["not_found"], [{"file": "a.json", "index": 99}])
        self.assertEqual([s["index"] for s in result["files"][0]["skipped"]], [1])
        self.assertTrue(result["files"][0]["skipped"][0]["reason"])

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
        self.assertEqual(runner.saved["a.json"][0]["trans_by"], "agent-model")
        self.assertEqual(runner.saved["a.json"][0]["pre_dst"], "新译文")
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
        self.assertEqual([s["index"] for s in result["files"][0]["skipped"]], [1])
        self.assertIn("pre_dst / proofread_dst", result["files"][0]["skipped"][0]["reason"])
        self.assertEqual(runner.saved["a.json"][0]["trans_by"], "agent-model")

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
        self.assertEqual(runner.saved["a.json"][0]["trans_by"], "deepseek-chat")


class MultiFilePatchTests(unittest.TestCase):
    """一次调用改多个缓存文件。

    以前只有"一个文件一次调用"：19 个文件要 19 次，中途被停止（用户点停止 / 进程被杀）
    就得模型自己记住"改到哪个文件了"。统一译名/术语这类活占复核工作量的一大半，正是这个形状。
    """

    def _runner(self) -> _StubRunner:
        return _StubRunner(
            {"a.json": [{"index": 1, "pre_dst": "小贤"}], "b.json": [{"index": 7, "pre_dst": "小贤"}]}
        )

    def test_one_call_covers_several_files(self) -> None:
        runner = self._runner()

        result = _tool_patch_transl_cache(
            runner,
            {
                "patches": [
                    {"file": "a.json", "index": 1, "pre_dst": "贤酱"},
                    {"file": "b.json", "index": 7, "pre_dst": "贤酱"},
                ]
            },
        )

        self.assertEqual(result["updated"], 2)
        self.assertEqual([item["filename"] for item in result["files"]], ["a.json", "b.json"])
        self.assertEqual(set(runner.saved), {"a.json", "b.json"})  # 两个文件都落盘了
        self.assertEqual(runner.saved["b.json"][0]["pre_dst"], "贤酱")
        self.assertEqual(result["trans_by"], "agent-model")

    def test_paths_carry_the_file_only_when_several_files_are_touched(self) -> None:
        runner = self._runner()

        one = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "z"}]}
        )
        self.assertEqual(one["changes"][0]["path"], "#1.pre_dst")  # 单文件：保持原来的短路径

        both = _tool_patch_transl_cache(
            runner,
            {
                "patches": [
                    {"file": "a.json", "index": 1, "pre_dst": "z2"},
                    {"file": "b.json", "index": 7, "pre_dst": "w"},
                ]
            },
        )
        # 跨文件：变更卡上要看得出这条改的是哪份文件
        self.assertEqual(
            [change["path"] for change in both["changes"]],
            ["a.json#1.pre_dst", "b.json#7.pre_dst"],
        )
        self.assertEqual({change["file"] for change in both["changes"]}, {"a.json", "b.json"})

    def test_top_level_filename_is_the_default_for_patches_without_file(self) -> None:
        """两种写法可以混用：不带 file 的那些走顶层 filename。"""
        runner = self._runner()

        result = _tool_patch_transl_cache(
            runner,
            {
                "filename": "a.json",
                "patches": [
                    {"index": 1, "pre_dst": "z"},
                    {"file": "b.json", "index": 7, "pre_dst": "w"},
                ],
            },
        )

        self.assertEqual(result["updated"], 2)
        self.assertEqual(runner.saved["a.json"][0]["pre_dst"], "z")
        self.assertEqual(runner.saved["b.json"][0]["pre_dst"], "w")

    def test_missing_file_is_an_error_not_a_guess(self) -> None:
        """两边都没给文件：报错说清怎么补，别猜一个文件改错地方。"""
        runner = self._runner()
        with self.assertRaises(AgentToolError) as ctx:
            _tool_patch_transl_cache(runner, {"patches": [{"index": 1, "pre_dst": "z"}]})
        self.assertIn("没写 file", str(ctx.exception))
        self.assertEqual(runner.saved, {})

    def test_one_bad_file_does_not_lose_the_others(self) -> None:
        """单个文件读不到（接口报错）不带走整批：另外一个照落，出错的那份报在结果里。"""
        runner = self._runner()
        original = runner._http_get

        def flaky(url: str):
            if "b.json" in url:
                raise AgentToolError("HTTP 500 internal")
            return original(url)

        runner._http_get = flaky

        result = _tool_patch_transl_cache(
            runner,
            {
                "patches": [
                    {"file": "a.json", "index": 1, "pre_dst": "z"},
                    {"file": "b.json", "index": 7, "pre_dst": "w"},
                ]
            },
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(set(runner.saved), {"a.json"})
        bad = next(item for item in result["files"] if item["filename"] == "b.json")
        self.assertIn("HTTP 500", bad["error"])

    def test_all_files_failing_still_raises(self) -> None:
        """一个文件都没改成：照旧整次报错（模型据此改入参），不是"部分成功"。"""
        runner = self._runner()
        with self.assertRaises(AgentToolError) as ctx:
            _tool_patch_transl_cache(
                runner,
                {
                    "patches": [
                        {"file": "a.json", "index": 99, "pre_dst": "z"},
                        {"file": "b.json", "index": 98, "pre_dst": "w"},
                    ]
                },
            )
        self.assertIn("没有条目被更新", str(ctx.exception))
        self.assertIn("a.json", str(ctx.exception))


class PatchMarkdownTests(unittest.TestCase):
    """给模型看的是 Markdown（按文件分节），JSON 只是内部结构。"""

    def test_renders_one_section_per_file(self) -> None:
        runner = _StubRunner(
            {
                "a.json": [{"index": 1, "pre_dst": "小贤", "problem": "残留日文"}],
                "b.json": [{"index": 7, "pre_dst": "小贤"}],
            }
        )
        result = _tool_patch_transl_cache(
            runner,
            {
                "patches": [
                    {"file": "a.json", "index": 1, "pre_dst": "贤酱"},
                    {"file": "b.json", "index": 7, "pre_dst": "贤酱"},
                    {"file": "b.json", "index": 99, "pre_dst": "不存在"},
                ]
            },
        )

        text = _render_tool_result_table("patch_transl_cache", result)

        self.assertIn("共改动 2 条，涉及 2 个缓存文件", text)
        self.assertIn("## 1. a.json（改 1 条）", text)
        self.assertIn("## 2. b.json（改 1 条）", text)
        self.assertIn("| a.json#1.pre_dst |", text)  # 跨文件时路径带文件名
        self.assertIn("没找到这些 index：99", text)  # 没落地的条目挂在对应文件下

    def test_problems_and_errors_are_listed_per_file(self) -> None:
        def rebuild(ents):
            for e in ents:
                e["problem"] = "仍有残留日文" if e.get("index") == 1 else ""
            return ents

        runner = _StubRunner({"a.json": [{"index": 1, "pre_dst": "x"}]}, rebuild=rebuild)
        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "y"}]}
        )

        text = _render_tool_result_table("patch_transl_cache", result)

        self.assertIn("改完仍存在的问题 1 条", text)
        self.assertIn("#1：仍有残留日文", text)


class ClearCommentTests(unittest.TestCase):
    """顶层 clear_comment=true：只写一次，就把点名条目的校对批注一并清空。

    背景：校对子代理把意见写进 proofread_comment，主 Agent 按意见改完译文后要清掉它表示"这条
    处理过了"。逐条写 `"proofread_comment": ""` 时，几十条意见就是几十个重复的键名——纯手续费。
    """

    def _runner(self) -> _StubRunner:
        return _StubRunner([
            {"index": 1, "pre_dst": "小贤", "proofread_comment": "建议改成「贤酱」"},
            {"index": 2, "pre_dst": "早上好", "proofread_comment": "润色：口语一点"},
            {"index": 9, "pre_dst": "没点名", "proofread_comment": "这条别动"},
        ])

    def test_clears_every_named_entry_and_leaves_others_alone(self) -> None:
        runner = self._runner()

        result = _tool_patch_transl_cache(
            runner,
            {
                "filename": "a.json",
                "clear_comment": True,
                "patches": [{"index": 1, "pre_dst": "贤酱"}, {"index": 2, "proofread_dst": "早上好啊"}],
            },
        )

        self.assertEqual(result["updated"], 2)
        saved = {int(e["index"]): e for e in runner.saved["a.json"]}
        self.assertEqual(saved[1]["proofread_comment"], "")
        self.assertEqual(saved[2]["proofread_comment"], "")
        self.assertEqual(saved[9]["proofread_comment"], "这条别动")  # 没点名的原样保留

    def test_clearing_is_visible_in_changes(self) -> None:
        """批注被清要看得见：变更卡片与结果表格都靠 changes，不能悄悄改。"""
        runner = self._runner()

        result = _tool_patch_transl_cache(
            runner,
            {"filename": "a.json", "clear_comment": True, "patches": [{"index": 1, "pre_dst": "贤酱"}]},
        )

        self.assertEqual(
            [(c["path"], c["before"], c["after"]) for c in result["changes"]],
            [("#1.pre_dst", "小贤", "贤酱"), ("#1.proofread_comment", "建议改成「贤酱」", "")],
        )

    def test_preview_shows_the_clearing_too(self) -> None:
        """审批卡上的 before→after 要和真执行一致——包括顺手清掉的批注。"""
        runner = self._runner()

        preview = _preview_cache_patch(
            runner,
            {"filename": "a.json", "clear_comment": True, "patches": [{"index": 1, "pre_dst": "贤酱"}]},
        )

        self.assertEqual(
            [(c["path"], c["after"]) for c in preview["changes"]],
            [("#1.pre_dst", "贤酱"), ("#1.proofread_comment", "")],
        )

    def test_explicit_comment_in_a_patch_wins(self) -> None:
        """某条 patch 自己写了批注就以它为准（含空串），不被开关覆盖。"""
        runner = self._runner()

        _tool_patch_transl_cache(
            runner,
            {
                "filename": "a.json",
                "clear_comment": True,
                "patches": [
                    {"index": 1, "pre_dst": "贤酱", "proofread_comment": "改成这样更好"},
                    {"index": 2, "pre_dst": "早上好"},
                ],
            },
        )

        saved = {int(e["index"]): e for e in runner.saved["a.json"]}
        self.assertEqual(saved[1]["proofread_comment"], "改成这样更好")
        self.assertEqual(saved[2]["proofread_comment"], "")

    def test_empty_comment_makes_no_noise_change(self) -> None:
        """本来就没有批注：不生成 `"" → ""` 这种没意义的变更行。"""
        runner = _StubRunner([{"index": 1, "pre_dst": "小贤"}])

        result = _tool_patch_transl_cache(
            runner,
            {"filename": "a.json", "clear_comment": True, "patches": [{"index": 1, "pre_dst": "贤酱"}]},
        )

        self.assertEqual([c["path"] for c in result["changes"]], ["#1.pre_dst"])

    def test_clearing_alone_is_a_real_update(self) -> None:
        """不改译文、只清批注也算一次有效写入（"这批意见不值得改，先把批注收掉"）。"""
        runner = self._runner()

        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "clear_comment": True, "patches": [{"index": 1}]}
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(runner.saved["a.json"][0]["proofread_comment"], "")

    def test_clearing_alone_does_not_stamp_trans_by(self) -> None:
        """只清批注、译文一字未动：别盖 trans_by（盖了会把"这句是谁翻的"弄错）。"""
        runner = _StubRunner([
            {"index": 1, "pre_dst": "小贤", "trans_by": "sakura", "proofread_comment": "有意见"},
        ])

        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "clear_comment": True, "patches": [{"index": 1}]}
        )

        self.assertNotIn("trans_by", result)
        self.assertEqual(runner.saved["a.json"][0]["trans_by"], "sakura")

    def test_nothing_to_clear_says_so(self) -> None:
        """没有批注可清：跳过原因要说清是"没批注"，别让模型以为 index 写错了。"""
        runner = _StubRunner([{"index": 1, "pre_dst": "小贤"}])

        with self.assertRaises(AgentToolError) as ctx:
            _tool_patch_transl_cache(
                runner, {"filename": "a.json", "clear_comment": True, "patches": [{"index": 1}]}
            )

        self.assertIn("批注", str(ctx.exception))
        self.assertEqual(runner.saved, {})

    def test_subagent_cannot_clear_comments(self) -> None:
        """校对子代理拿同一个 handler：它只写批注，不该顺手清掉还没处理的意见。"""
        runner = self._runner()

        result = _tool_patch_transl_cache(
            runner,
            {
                "filename": "a.json",
                "clear_comment": True,
                "patches": [{"index": 1, "proofread_comment": "新意见"}, {"index": 2}],
            },
            SUBAGENT_PATCHABLE_FIELDS,
        )

        saved = {int(e["index"]): e for e in runner.saved["a.json"]}
        self.assertEqual(saved[1]["proofread_comment"], "新意见")
        self.assertEqual(saved[2]["proofread_comment"], "润色：口语一点")  # 没被开关清掉
        self.assertEqual([s["index"] for s in result["files"][0]["skipped"]], [2])


if __name__ == "__main__":
    unittest.main()
