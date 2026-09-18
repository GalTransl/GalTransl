"""ask_user（询问用户）的语义测试。

工具语义：

- 工具**阻塞**等回答，没有超时；
- 用户跳过、或回合被停止时，该题以空答案返回——工具本身仍算成功，模型据此继续，
  而不是让整个回合报错；
- 答案要校验：题数必须一致、单选不许给多个值、trim + 去重；
- **没有在等的询问时不报错**：进程在提问上被杀（用户关了程序再打开）会留下"有
  tool_calls、缺 tool 响应"的残缺历史，卡片却还在转录里。这时把用户的选择当成一条
  用户消息继续（见 AgentRuntime._answer_ask_as_message），并给残缺调用补上占位响应
  / 失败事件（见 DanglingToolCallTests）。
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    ASK_MAX_QUESTIONS,
    AUTO_QUIET_MODE,
    AgentRunner,
    AgentRuntime,
    AgentState,
    AgentToolError,
    _TOOL_HANDLERS,
    _dangling_tool_calls,
    _format_ask_answers,
    _messages_with_tool_placeholders,
    _normalize_ask_answers,
    _normalize_ask_questions,
    _permission_decision_text,
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
            {
                "questions": [
                    {
                        "question": "  用哪种译法？  ",
                        "options": ["A", " A ", "B", ""],
                        "multiSelect": True,
                        "recommended": " B ",
                    }
                ]
            }
        )
        self.assertEqual(
            questions,
            [
                {
                    "question": "用哪种译法？",
                    "options": ["A", "B"],
                    "multiSelect": True,
                    "recommended": "B",
                }
            ],
        )

    def test_multi_select_defaults_false_and_recommended_defaults_empty(self) -> None:
        questions = _normalize_ask_questions({"questions": [{"question": "Q", "options": ["A"]}]})
        self.assertFalse(questions[0]["multiSelect"])
        self.assertEqual(questions[0]["recommended"], "")

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
            # recommended 给了但不在 options 里：让它改，别默默丢掉（零打断档位靠它代答）
            {"questions": [{"question": "Q", "options": ["A", "B"], "recommended": "C"}]},
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

    def test_answer_without_pending_ask_resumes_as_a_user_message(self) -> None:
        """重启后只剩一张重建出来的卡片：这次选择当成用户消息继续，不再报错。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        # 历史停在一次 ask_user 上（进程就是在这儿被杀掉的）：tool_calls 有、tool 响应没有
        ss.SessionStore(self.project, sid).append_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {"questions": [{"question": "用哪种？", "options": ["A", "B"]}]},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        )

        with patch.object(AgentRunner, "run", lambda self: None):  # 不起真回合
            out = rt.answer_ask(
                self.project, sid, [["A"]], backend_profile_data={"tokens": "t"}
            )

        self.assertTrue(out["ok"])
        self.assertTrue(out["resumed_as_message"])
        # 另起回合要靠请求里的 token（会话状态不落盘）：前端上下文要跟着这次答复过来
        self.assertEqual(rt._get_state(self.project, sid).backend_profile_data, {"tokens": "t"})
        messages = ss.SessionStore(self.project, sid).load()["messages"]
        self.assertEqual(messages[-1]["role"], "user")
        # 题目从残缺的 ask_user 调用里取回，渲染口径与工具结果一致
        self.assertIn("用哪种？：A", messages[-1]["content"])
        # 收卡片的 tool_result 必须排在 user_message 之前：反过来的话前端会把它挂到
        # 新开的那一段上去（user_message 会另起一段）。用 load() 看落盘顺序——
        # read_transcript 会把"首条 user_message"提到最前当身份锚点，看不出真实先后。
        types = [event["type"] for event in ss.SessionStore(self.project, sid).load()["events"]]
        self.assertLess(types.index("tool_result"), types.index("user_message"))

    def test_answer_on_an_empty_session_still_errors(self) -> None:
        """会话连一条消息都没有：没有历史可续，照旧报错（前端应先发第一条消息）。"""
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]
        with self.assertRaises(ValueError):
            rt.answer_ask(self.project, sid, [["A"]])

    def test_answer_without_any_session_errors(self) -> None:
        rt = AgentRuntime()
        with self.assertRaises(ValueError):
            rt.answer_ask(self.project, None, [["A"]])


class DanglingToolCallTests(unittest.TestCase):
    """进程在工具执行中途被杀留下的残缺历史：请求侧补占位、界面侧补失败事件。

    不补的话：请求发给 provider 直接 400（带 tool_calls 的 assistant 后面必须紧跟
    每个调用的 tool 响应），而界面上那张 ask_user / 审批卡永远挂着、点了就被后端告知
    "没有在等的问题"。
    """

    @staticmethod
    def _runner(messages: list[dict]) -> AgentRunner:
        state = AgentState()
        state.session_id = ""  # 不落盘
        state.messages = list(messages)
        return AgentRunner(state)

    @staticmethod
    def _assistant(*call_ids: str) -> dict:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "ask_user", "arguments": "{}"}}
                for call_id in call_ids
            ],
        }

    def test_detects_calls_without_a_tool_response(self) -> None:
        dangling = _dangling_tool_calls([self._assistant("c1")])

        self.assertEqual([item["id"] for item in dangling], ["c1"])
        self.assertEqual(dangling[0]["name"], "ask_user")
        self.assertEqual(dangling[0]["at"], 1)  # 占位该插的位置：紧随这条 assistant

    def test_answered_calls_are_not_dangling(self) -> None:
        messages = [
            self._assistant("c1"),
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "user", "content": "继续"},
        ]

        self.assertEqual(_dangling_tool_calls(messages), [])
        # 没有残缺就原样返回（省一次整份拷贝）
        self.assertIs(_messages_with_tool_placeholders(messages), messages)

    def test_placeholders_go_after_the_existing_tool_batch(self) -> None:
        """两个调用只答了一个：占位插在已有 tool 响应之后，保持 tool 消息连续。"""
        messages = [
            self._assistant("c1", "c2"),
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "user", "content": "继续"},
        ]

        patched = _messages_with_tool_placeholders(messages)

        self.assertEqual([m["role"] for m in patched], ["assistant", "tool", "tool", "user"])
        self.assertEqual(patched[2]["tool_call_id"], "c2")
        self.assertEqual(len(messages), 3)  # 原历史一份不动（落盘是追加式的）

    def test_messages_for_request_gets_the_placeholder(self) -> None:
        """发给 provider 的那份必须合法：残缺调用在请求里补上占位 tool 消息。"""
        runner = self._runner([self._assistant("c1"), {"role": "user", "content": "继续"}])

        messages = runner._messages_for_request()

        self.assertEqual([m["role"] for m in messages], ["assistant", "tool", "user"])
        self.assertEqual(messages[1]["tool_call_id"], "c1")
        self.assertEqual(len(runner.state.messages), 2)  # 原历史一份不动

    def test_close_dangling_tool_calls_emits_once(self) -> None:
        runner = self._runner([self._assistant("c1")])

        runner._close_dangling_tool_calls()
        runner._close_dangling_tool_calls()  # 同一进程里只补一次

        results = [event for event in runner.state.events if event.type == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].data["id"], "c1")
        self.assertFalse(results[0].data["ok"])
        self.assertIn("没有执行完", results[0].data["error"])

    def test_permission_decision_text(self) -> None:
        self.assertEqual(
            _permission_decision_text("deny", "start_translation", "现在不要动"),
            "我不同意执行「start_translation」，这一步别做了。原因：现在不要动",
        )
        self.assertEqual(
            _permission_decision_text("allow-once", "start_translation", ""),
            "我同意执行「start_translation」，请继续。",
        )
        self.assertEqual(
            _permission_decision_text("allow-once", "", ""),
            "我同意执行上面那步操作，请继续。",
        )


class DanglingToolCallAcrossTurnsTests(unittest.TestCase):
    """残缺调用要"只补一次"且要补在用户消息**之前**——重启也算同一条。

    回归背景（实测）：那条 ask_user 在历史里永远是残缺的（占位结果只补在请求侧、不写回
    历史），重启后 runner 换了实例、已收集合是空的，于是每开一个新回合都把它再判一次
    "这一步没有执行完"；而这条 tool_result 落在 user_message **之后**，前端找不到原来的
    工具行，只能新建一段挂到最后——用户点「继续」看到的就是这张"刚失败"的旧卡。
    """

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-dangling-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-dangling-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)
        self.sid = ss.create_session(self.project)
        # 历史停在那次 ask_user 上：tool_calls 有、tool 响应没有（进程就是在这儿被杀的）
        ss.SessionStore(self.project, self.sid).append_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {"questions": [{"question": "用哪种？", "options": ["A", "B"]}]},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        )

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def _restarted_runner(self) -> AgentRunner:
        """按落盘历史造一个 runner：等价于（重）启动后恢复出来的那份状态。"""
        state = AgentState(session_id=self.sid, project_dir=self.project)
        state.messages = ss.SessionStore(self.project, self.sid).load()["messages"]
        return AgentRunner(state)

    @staticmethod
    def _results(runner: AgentRunner) -> list:
        return [event for event in runner.state.events if event.type == "tool_result"]

    def test_restart_does_not_announce_the_same_call_again(self) -> None:
        first = self._restarted_runner()
        first._close_dangling_tool_calls()
        self.assertEqual(len(self._results(first)), 1)  # 第一次收：事件落盘

        again = self._restarted_runner()  # 重启：新 runner、已收集合空的、历史依旧残缺
        again._close_dangling_tool_calls()

        self.assertEqual(self._results(again), [])  # 认回落盘的那条，不再重复判失败

    def test_stale_card_is_closed_before_the_new_user_message(self) -> None:
        runtime = AgentRuntime()
        runner = self._restarted_runner()
        key = runtime._key(self.project)
        with runtime._lock:
            runtime._states.setdefault(key, {})[self.sid] = runner.state
            runtime._runners.setdefault(key, {})[self.sid] = runner
            runtime._stop_events.setdefault(key, {})[self.sid] = threading.Event()

        with patch.object(AgentRunner, "run", lambda self: None):  # 不起真回合
            runtime.message(self.project, "继续", self.sid)

        types = [event["type"] for event in ss.SessionStore(self.project, self.sid).load()["events"]]
        self.assertLess(types.index("tool_result"), types.index("user_message"))


class AutoQuietAutoAnswerTests(unittest.TestCase):
    """「全自动-零打断」：ask_user 不再阻塞等人，后端按每题的推荐项代答。

    推荐项缺失时退而取第一个选项——这一档的语义就是"别停下来问我"，卡在等人作答上
    比偶尔选歪一次更糟。其余档位照常阻塞（回归见 AskUserBlockingTests）。
    """

    @staticmethod
    def _runner(mode: str) -> AgentRunner:
        state = AgentState()
        state.session_id = ""  # 不落盘
        state.permission_mode = mode
        return AgentRunner(state)

    def test_picks_the_recommended_option_without_blocking(self) -> None:
        runner = self._runner(AUTO_QUIET_MODE)

        out = _tool_ask_user(
            runner,
            {"questions": [{"question": "用哪种？", "options": ["A", "B"], "recommended": "B"}]},
        )

        self.assertEqual(out["answers"], [["B"]])
        self.assertTrue(out["auto_answered"])
        self.assertIsNone(runner._pending_ask)  # 从未挂起
        self.assertIn("零打断", out["summary"])

    def test_falls_back_to_the_first_option(self) -> None:
        runner = self._runner(AUTO_QUIET_MODE)

        out = _tool_ask_user(runner, {"questions": [{"question": "Q", "options": ["A", "B"]}]})

        self.assertEqual(out["answers"], [["A"]])
        self.assertIsNone(runner._pending_ask)

    def test_answers_every_question_of_a_multi_question_ask(self) -> None:
        runner = self._runner(AUTO_QUIET_MODE)

        out = _tool_ask_user(
            runner,
            {
                "questions": [
                    {"question": "Q1", "options": ["A", "B"], "recommended": "B"},
                    {"question": "Q2", "options": ["X", "Y"], "recommended": "Y"},
                ]
            },
        )

        self.assertEqual(out["answers"], [["B"], ["Y"]])

    def test_other_modes_still_block(self) -> None:
        """零打断是唯一代答的档位：其余档位照旧阻塞，等人作答。"""
        for mode in ("ask", "accept-edits", "auto"):
            runner = self._runner(mode)
            done = threading.Event()

            def worker(r: AgentRunner = runner) -> None:
                _tool_ask_user(r, {"questions": [dict(SINGLE)]})
                done.set()

            threading.Thread(target=worker, daemon=True).start()
            _wait_pending(runner)  # 挂起来了 = 没有自动作答
            runner.resolve_ask([["A"]])

            self.assertTrue(done.wait(2), mode)


class AskToolExposureTests(unittest.TestCase):
    def test_tool_is_offered_to_the_model(self) -> None:
        names = {tool["function"]["name"] for tool in AGENT_TOOLS}
        self.assertIn("ask_user", names)
        self.assertIn("ask_user", _TOOL_HANDLERS)
        schema = next(t["function"] for t in AGENT_TOOLS if t["function"]["name"] == "ask_user")
        props = schema["parameters"]["properties"]["questions"]["items"]["properties"]
        self.assertEqual(set(props), {"question", "options", "multiSelect", "recommended"})


if __name__ == "__main__":
    unittest.main()
