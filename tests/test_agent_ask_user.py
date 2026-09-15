"""ask_user（询问用户）的语义测试。

工具语义：

- 工具**阻塞**等回答，没有超时；
- 用户跳过、或回合被停止时，该题以空答案返回——工具本身仍算成功，模型据此继续，
  而不是让整个回合报错；
- 答案要校验：题数必须一致、单选不许给多个值、trim + 去重。
"""

import os
import tempfile
import threading
import time
import unittest

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    ASK_MAX_QUESTIONS,
    AgentRunner,
    AgentRuntime,
    AgentState,
    AgentToolError,
    _TOOL_HANDLERS,
    _format_ask_answers,
    _normalize_ask_answers,
    _normalize_ask_questions,
    _tool_ask_user,
)

SINGLE = {"question": "用哪种？", "options": ["A", "B"], "multiSelect": False}


def _wait_pending(runner: AgentRunner, timeout: float = 2.0) -> None:
    """等工具真正挂起（避免测试抢在注册之前就去 resolve）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with runner._ask_lock:
            if runner._pending_ask is not None:
                return
        time.sleep(0.01)
    raise AssertionError("工具没有进入等待状态")


class AskQuestionValidationTests(unittest.TestCase):
    def test_normalizes_trims_and_dedupes(self) -> None:
        questions = _normalize_ask_questions(
            {"questions": [{"question": "  用哪种译法？  ", "options": ["A", " A ", "B", ""], "multiSelect": True}]}
        )
        self.assertEqual(
            questions,
            [{"question": "用哪种译法？", "options": ["A", "B"], "multiSelect": True}],
        )

    def test_multi_select_defaults_false(self) -> None:
        questions = _normalize_ask_questions({"questions": [{"question": "Q", "options": ["A"]}]})
        self.assertFalse(questions[0]["multiSelect"])

    def test_rejects_bad_shapes(self) -> None:
        bad_cases = [
            {},
            {"questions": []},
            {"questions": "Q"},
            {"questions": ["Q"]},
            {"questions": [{"question": "", "options": ["A"]}]},
            {"questions": [{"question": "Q"}]},
            {"questions": [{"question": "Q", "options": []}]},
            {"questions": [{"question": "Q", "options": ["", " "]}]},
        ]
        for bad in bad_cases:
            with self.assertRaises(AgentToolError):
                _normalize_ask_questions(bad)

    def test_caps_question_count(self) -> None:
        too_many = {"questions": [{"question": f"Q{i}", "options": ["A"]} for i in range(ASK_MAX_QUESTIONS + 1)]}
        with self.assertRaises(AgentToolError):
            _normalize_ask_questions(too_many)


class AskAnswerValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.questions = [SINGLE, {"question": "平台", "options": ["X", "Y"], "multiSelect": True}]

    def test_accepts_matching_shapes(self) -> None:
        self.assertEqual(
            _normalize_ask_answers([["A"], ["X", "Y"]], self.questions),
            [["A"], ["X", "Y"]],
        )

    def test_empty_or_null_means_skipped(self) -> None:
        """跳过 = 不勾任何项（空数组）或 null，都给到 None。"""
        self.assertEqual(_normalize_ask_answers([None, ["X"]], self.questions), [None, ["X"]])
        self.assertEqual(_normalize_ask_answers([[], []], self.questions), [None, None])

    def test_trims_and_dedupes(self) -> None:
        self.assertEqual(
            _normalize_ask_answers([[" A ", "A"], ["X", "X"]], self.questions),
            [["A"], ["X"]],
        )

    def test_rejects_wrong_count(self) -> None:
        for bad in ([["A"]], [["A"], ["X"], ["Y"]], "A", None):
            with self.assertRaises(AgentToolError):
                _normalize_ask_answers(bad, self.questions)

    def test_rejects_multiple_values_on_single_select(self) -> None:
        with self.assertRaises(AgentToolError):
            _normalize_ask_answers([["A", "B"], ["X"]], self.questions)

    def test_rejects_non_array_answer(self) -> None:
        with self.assertRaises(AgentToolError):
            _normalize_ask_answers(["A", ["X"]], self.questions)


class AskFormatTests(unittest.TestCase):
    def test_summary_uses_pi_wording(self) -> None:
        text = _format_ask_answers([SINGLE, {"question": "平台"}], [["A", "B"], None])
        self.assertEqual(text, "用哪种？：A、B\n---\n平台：（跳过）")


class AskUserBlockingTests(unittest.TestCase):
    """工具阻塞语义：等到答案才返回；被停止则按跳过返回。"""

    @staticmethod
    def _runner() -> AgentRunner:
        state = AgentState()
        state.session_id = ""  # 不落盘
        return AgentRunner(state)

    def test_blocks_until_answered(self) -> None:
        runner = self._runner()
        box: dict = {}
        done = threading.Event()

        def worker() -> None:
            box["result"] = _tool_ask_user(runner, {"questions": [dict(SINGLE)]})
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        _wait_pending(runner)

        runner.resolve_ask([["B"]])

        self.assertTrue(done.wait(2), "拿到答案后工具应该立刻返回")
        self.assertEqual(box["result"]["answers"], [["B"]])
        self.assertEqual(box["result"]["summary"], "用哪种？：B")
        self.assertIsNone(runner._pending_ask)  # 收尾后不再挂着

    def test_stop_returns_skipped(self) -> None:
        runner = self._runner()
        box: dict = {}
        done = threading.Event()

        def worker() -> None:
            box["result"] = _tool_ask_user(runner, {"questions": [dict(SINGLE)]})
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        _wait_pending(runner)

        runner.stop_event.set()  # 用户点了停止 / 会话被删

        self.assertTrue(done.wait(2), "停止信号应立刻打断等待")
        self.assertEqual(box["result"]["answers"], [None])
        self.assertEqual(box["result"]["summary"], "用哪种？：（跳过）")
        self.assertIsNone(runner._pending_ask)

    def test_bad_args_never_block(self) -> None:
        runner = self._runner()
        with self.assertRaises(AgentToolError):
            _tool_ask_user(runner, {"questions": [{"question": "Q", "options": []}]})
        self.assertIsNone(runner._pending_ask)

    def test_resolve_without_pending_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._runner().resolve_ask([["A"]])

    def test_resolve_rejects_bad_answers_and_keeps_waiting(self) -> None:
        runner = self._runner()
        done = threading.Event()

        def worker() -> None:
            _tool_ask_user(runner, {"questions": [dict(SINGLE)]})
            done.set()

        threading.Thread(target=worker, daemon=True).start()
        _wait_pending(runner)

        with self.assertRaises(ValueError):
            runner.resolve_ask([["A", "B"]])  # 单选给了两个
        with self.assertRaises(ValueError):
            runner.resolve_ask([])  # 题数不匹配

        with runner._ask_lock:
            self.assertIsNotNone(runner._pending_ask)  # 还挂着，等一个合法答案
        runner.resolve_ask([["A"]])
        self.assertTrue(done.wait(2))


class RegistryAnswerAskTests(unittest.TestCase):
    """HTTP 层走的就是 AgentRuntime.answer_ask：路由到对应 runner，没在等就问不了。"""

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-ask-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-ask-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_answer_reaches_the_waiting_runner(self) -> None:
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        runner = AgentRunner(AgentState(session_id=sid, project_dir=self.project))
        holder = {
            "request_id": "r1",
            "tool_call_id": "c1",
            "questions": [dict(SINGLE)],
            "answers": None,
            "event": threading.Event(),
        }
        with runner._ask_lock:
            runner._pending_ask = holder
        with rt._lock:
            rt._runners.setdefault(rt._key(self.project), {})[sid] = runner

        out = rt.answer_ask(self.project, sid, [["A"]])

        self.assertTrue(out["ok"])
        self.assertEqual(holder["answers"], [["A"]])
        self.assertTrue(holder["event"].is_set())

    def test_answer_without_pending_ask_raises(self) -> None:
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with self.assertRaises(ValueError):
            rt.answer_ask(self.project, sid, [["A"]])

    def test_answer_without_runner_raises(self) -> None:
        rt = AgentRuntime()
        with self.assertRaises(ValueError):
            rt.answer_ask(self.project, None, [["A"]])


class AskToolExposureTests(unittest.TestCase):
    def test_tool_is_offered_to_the_model(self) -> None:
        names = {tool["function"]["name"] for tool in AGENT_TOOLS}
        self.assertIn("ask_user", names)
        self.assertIn("ask_user", _TOOL_HANDLERS)
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "ask_user")
        props = schema["parameters"]["properties"]["questions"]["items"]["properties"]
        self.assertEqual(set(props), {"question", "options", "multiSelect"})


if __name__ == "__main__":
    unittest.main()
