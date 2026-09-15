"""get_runtime 的 recent_errors 要"增量 + 同类合并"下发。

背景（两轮踩过的坑）：

1. 后端快照里的 recent_errors 是滑动窗口，同一条错误（某个文件的 parse warning）会在
   窗口里待很久。Agent 每次拿到完整列表，于是每次都重新判断"这是不是新错误"——同一条
   sc_2_st10 的 warning 连着 5 次以上被要求重新判断。
2. 真实报错是**同一类原因反复出现**（kind=parse 的「未解析到有效句子」横跨几十个文件
   几十个条目），逐条发只会刷屏。

所以现在的契约是：发过的错误记水位线、只发"上次查询之后新出现的"，同类归并成
「xxx错误 × N 次」一条（count = 本次新增次数，text 是一行摘要，files 是涉及的文件），
单次最多 10 类；没轮到的组留到下次（既不刷屏也不漏），列表为空时说明"只是没有新错误"。
"""

import os
import tempfile
import unittest

from GalTransl.Agent.runtime import (
    RUNTIME_ERRORS_PER_QUERY,
    AgentState,
    _tool_get_runtime,
)


def _err(
    err_id: str,
    message: str = "未解析到有效句子",
    filename: str = "sc_2_st10.json",
    ts: str = "2026-09-15 10:00:00",
    kind: str = "parse",
    level: str = "warning",
) -> dict:
    return {
        "id": err_id,
        "ts": ts,
        "kind": kind,
        "level": level,
        "message": message,
        "filename": filename,
        "index_range": "1-3",
    }


class _Runner:
    """最小 runner：/runtime 返回当前快照里的错误列表（外部可随时追加/替换）。"""

    def __init__(self, errors: list[dict] | None = None) -> None:
        self.state = AgentState()
        self.errors = list(errors or [])

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, path: str):
        assert path.endswith("/runtime"), path
        return {
            "job": {"status": "running", "translator": "ForGal-json"},
            "stage": "translate",
            "current_file": "sc_2_st10.json",
            "summary": {"total": 100, "translated": 50, "percent": 50.0},
            "recent_errors": list(self.errors),
        }


class ErrorMergingTests(unittest.TestCase):
    def test_identical_errors_are_merged_into_one_line(self) -> None:
        """同一类报错横跨 12 个文件报 23 次 → 一条「parse 警告 × 23 次」。"""
        errors = [
            _err(f"e{i}", filename=f"sc_2_st{i % 12}.json") for i in range(23)
        ]
        out = _tool_get_runtime(_Runner(errors), {})

        self.assertEqual(len(out["recent_errors"]), 1)
        group = out["recent_errors"][0]
        self.assertEqual(group["count"], 23)
        self.assertEqual(group["kind"], "parse")
        self.assertEqual(group["level"], "warning")
        self.assertEqual(group["files_total"], 12)
        self.assertLessEqual(len(group["files"]), 5)  # 文件列表有上限

        text = group["text"]
        self.assertIn("parse 警告 × 23 次", text)
        self.assertIn("未解析到有效句子", text)
        self.assertIn("12 个", text)  # 涉及多少个文件

    def test_different_reasons_stay_separate(self) -> None:
        out = _tool_get_runtime(
            _Runner(
                [
                    _err("a", message="未解析到有效句子"),
                    _err("b", message="JSON 解析失败"),
                    _err("c", message="未解析到有效句子", kind="timeout", level="error"),
                ]
            ),
            {},
        )
        pairs = {(g["kind"], g["message"]) for g in out["recent_errors"]}
        self.assertEqual(
            pairs,
            {
                ("parse", "未解析到有效句子"),
                ("parse", "JSON 解析失败"),
                ("timeout", "未解析到有效句子"),
            },
        )  # kind / level / message 任一不同就不合并
        texts = [g["text"] for g in out["recent_errors"]]
        self.assertTrue(any(t.startswith("parse 警告 × 1 次") for t in texts))
        self.assertTrue(any(t.startswith("timeout 错误 × 1 次") for t in texts))

    def test_filename_embedded_in_message_is_placeholder(self) -> None:
        """message 里嵌了本文件的文件名 → 换成 {file}，别被文件名拆成十几组。"""
        out = _tool_get_runtime(
            _Runner(
                [
                    _err("a", message="sc_2_st10.json 第 3 行解析异常", filename="sc_2_st10.json"),
                    _err("b", message="sc_2_st11.json 第 3 行解析异常", filename="sc_2_st11.json"),
                ]
            ),
            {},
        )
        self.assertEqual(len(out["recent_errors"]), 1)
        group = out["recent_errors"][0]
        self.assertEqual(group["count"], 2)
        self.assertEqual(group["files_total"], 2)
        self.assertIn("{file}", group["message"])

    def test_more_than_cap_groups_wait_for_next_query(self) -> None:
        errors = [_err(f"e{i}", message=f"第 {i} 类错误") for i in range(15)]
        runner = _Runner(errors)

        out = _tool_get_runtime(runner, {})
        self.assertEqual(len(out["recent_errors"]), RUNTIME_ERRORS_PER_QUERY)
        self.assertEqual(out["recent_errors_pending"], 5)  # 还有 5 条新错误没发

        rest = _tool_get_runtime(runner, {})
        self.assertEqual(len(rest["recent_errors"]), 5)
        self.assertNotIn("recent_errors_pending", rest)
        # 两次发出来的类合起来正好 15 类，不重不漏
        got = {g["message"] for g in out["recent_errors"]} | {g["message"] for g in rest["recent_errors"]}
        self.assertEqual(got, {f"第 {i} 类错误" for i in range(15)})


class IncrementalRecentErrorsTests(unittest.TestCase):
    def test_first_query_reports_current_errors(self) -> None:
        out = _tool_get_runtime(_Runner([_err("e1")]), {})
        self.assertEqual([g["count"] for g in out["recent_errors"]], [1])
        self.assertNotIn("recent_errors_pending", out)
        self.assertIn("新出现", out["recent_errors_note"])
        self.assertIn("count", out["recent_errors_note"])

    def test_repeated_queries_do_not_repeat_the_same_error(self) -> None:
        """用户遇到的场景：同一条 warning 在连续 5 次查询里反复出现。"""
        runner = _Runner([_err("sc_2_st10")])
        first = _tool_get_runtime(runner, {})
        self.assertEqual(len(first["recent_errors"]), 1)
        for _ in range(4):
            again = _tool_get_runtime(runner, {})
            self.assertEqual(again["recent_errors"], [])
            self.assertIn("只代表没有新错误", again["recent_errors_note"])

    def test_group_count_only_counts_new_occurrences(self) -> None:
        """第二页重复出现的同一类：count 是本次新增，不重复累计。"""
        runner = _Runner([_err("a"), _err("b")])
        self.assertEqual(_tool_get_runtime(runner, {})["recent_errors"][0]["count"], 2)

        runner.errors = [_err("c"), _err("d", filename="sc_2_st11.json")]
        out = _tool_get_runtime(runner, {})
        self.assertEqual(out["recent_errors"][0]["count"], 2)  # 只有新增的这两条
        self.assertEqual(out["recent_errors"][0]["files_total"], 2)

    def test_watermark_does_not_grow_without_bound(self) -> None:
        """滚出快照的错误不会再回来，水位线不该一直存着它们。"""
        runner = _Runner([_err(f"old{i}", message=f"消息{i}") for i in range(6)])
        _tool_get_runtime(runner, {})
        self.assertEqual(len(runner.state.seen_error_ids), 6)

        runner.errors = [_err("new1", message="新消息")]
        out = _tool_get_runtime(runner, {})
        self.assertEqual([g["message"] for g in out["recent_errors"]], ["新消息"])
        self.assertEqual(len(runner.state.seen_error_ids), 1)

    def test_errors_without_id_dedupe_by_content(self) -> None:
        """缺 id 时按内容去重：同一条仍只报一次，新内容照常报。"""
        runner = _Runner([_err(""), _err("", message="另一条")])
        out = _tool_get_runtime(runner, {})
        self.assertEqual(len(out["recent_errors"]), 2)

        again = _tool_get_runtime(runner, {})
        self.assertEqual(again["recent_errors"], [])

    def test_summary_scale_note_still_present(self) -> None:
        """顺带确认口径说明没被这次改动挤掉。"""
        out = _tool_get_runtime(_Runner([_err("e1")]), {})
        self.assertIn("两个 total 分母不同", out["summary_note"])
        self.assertEqual(out["summary"]["total"], 100)


class RealRuntimePayloadTests(unittest.TestCase):
    """用真实 RuntimeRegistry 产生的 recent_errors 载荷验一遍（id 字段与形状）。

    单测里的假载荷是手写的，这里换成 get_runtime_snapshot 的真实输出，确认水位线能落到
    后端真正给出的 id 上——否则"发过的不再重复"只是对着假数据成立。
    """

    def test_real_snapshot_errors_are_reported_once(self) -> None:
        try:
            from GalTransl.server_runtime import RuntimeRegistry
        except ModuleNotFoundError:  # 精简环境（如系统 python）没有 yaml，跳过
            self.skipTest("server_runtime 依赖不可用")

        registry = RuntimeRegistry()
        project = os.path.join(tempfile.mkdtemp(prefix="agent-rt-errors-"), "MyGame")
        for i in range(3):
            registry.append_error(
                project,
                kind="parse",
                message="未解析到有效句子",
                filename=f"sc_2_st1{i}.json",
                index_range=f"{i + 1}-{i + 1}",
                level="warning",
            )

        real_errors = registry.get_runtime_snapshot(project)["recent_errors"]
        assert all(e.get("id") for e in real_errors), "后端载荷必须带 id，水位线靠它"

        class _RealRunner:
            def __init__(self) -> None:
                self.state = AgentState()

            def _project_id(self) -> str:
                return "proj"

            def _http_get(self, path: str):
                assert path.endswith("/runtime"), path
                job = {"status": "running", "translator": "ForGal-json"}
                return {"job": job, **registry.get_runtime_snapshot(project)}

        runner = _RealRunner()
        first = _tool_get_runtime(runner, {})
        self.assertEqual(len(first["recent_errors"]), 1)  # 3 条同类 → 一条
        self.assertEqual(first["recent_errors"][0]["count"], 3)
        self.assertEqual(first["recent_errors"][0]["files_total"], 3)

        for _ in range(4):  # 用户场景：连续 5 次查询
            self.assertEqual(_tool_get_runtime(runner, {})["recent_errors"], [])

        self.assertEqual(len(runner.state.seen_error_ids), 3)


if __name__ == "__main__":
    unittest.main()
