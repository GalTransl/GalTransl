"""Agent 的权限门禁：模式 × 风险矩阵、审批阻塞、三种答复的后果。

对照参考实现（PI-Desktop 的 permission mode）：
- 三档模式 ask / accept-edits / auto，默认 ask；
- 工具分 read / edit / high 三档风险，**没登记的按 high**（fail closed）；
- ask：写操作一律先问；accept-edits：只自动放行"改译文数据"（缓存/字典/人名表）；
  auto：全放行；
- 答复只有 allow-once / allow-session（按工具名，本会话有效）/ deny；
- 超时当拒绝（fail closed），拒绝时给模型一条"用户拒绝权限"的工具错误。
"""

import threading
import time
import unittest
from unittest.mock import patch

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    DEFAULT_PERMISSION_MODE,
    PERMISSION_EDIT,
    PERMISSION_HIGH,
    PERMISSION_MODES,
    PERMISSION_READ,
    PERMISSION_READ_TOOLS,
    PERMISSION_TOOL_RISK,
    AgentRunner,
    AgentRuntime,
    AgentState,
    AgentToolError,
    _normalize_permission_mode,
    _permission_denied_reason,
    _permission_needed,
    _tool_risk,
)


def make_runner(mode: str = "ask", grants: tuple[str, ...] = ()) -> AgentRunner:
    # 不给 session_id：AgentRunner 就不建 SessionStore，测试不会往磁盘写会话文件
    state = AgentState(project_dir=r"C:\proj", permission_mode=mode)
    state.permission_grants.update(grants)
    return AgentRunner(state)


def run_gate(runner: AgentRunner, name: str, decision: str | None, *, dispatch: bool = False) -> dict:
    """子线程里跑一次门禁（或整条 _dispatch_tool），等它挂起后作答。

    decision=None 表示只等挂起、不作答（用于测校验分支）。返回 {ok/error/alive}。
    """
    out: dict = {}

    def target() -> None:
        try:
            if dispatch:
                runner._dispatch_tool(name, {"filename": "a.json", "reason": "试试"})
            else:
                runner._require_permission(name, {"filename": "a.json", "reason": "试试"})
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with runner._perm_lock:
            if runner._pending_permission is not None:
                break
        time.sleep(0.01)
    if decision is not None:
        runner.resolve_permission(decision)
    # 作答了才值得等它跑完；只等挂起的那些（decision=None）别把 3 秒白等掉
    thread.join(timeout=3 if decision is not None else 0.05)
    out["alive"] = thread.is_alive()
    return out


class PermissionMatrixTests(unittest.TestCase):
    def test_matrix(self) -> None:
        """读永远放行；编辑在 ask 要问、其余档放行；高风险只有 auto 放行。"""
        cases = {
            (PERMISSION_READ, "ask"): False,
            (PERMISSION_READ, "accept-edits"): False,
            (PERMISSION_READ, "auto"): False,
            (PERMISSION_EDIT, "ask"): True,
            (PERMISSION_EDIT, "accept-edits"): False,
            (PERMISSION_EDIT, "auto"): False,
            (PERMISSION_HIGH, "ask"): True,
            (PERMISSION_HIGH, "accept-edits"): True,
            (PERMISSION_HIGH, "auto"): False,
        }
        for (risk, mode), expected in cases.items():
            self.assertEqual(_permission_needed(risk, mode), expected, f"{risk}/{mode}")

    def test_risk_classification(self) -> None:
        # 改译文数据：缓存 / 字典 / 人名表 —— "允许编辑"档放行的就是这些
        for name in ("patch_transl_cache", "delete_transl_cache", "save_dict", "create_dict_file", "save_name_table"):
            self.assertEqual(_tool_risk(name), PERMISSION_EDIT, name)
        # 改设置 / 规范、启动任务：只有全自动放行
        for name in ("update_project_config", "manage_problem_filter", "write_project_guideline", "start_translation"):
            self.assertEqual(_tool_risk(name), PERMISSION_HIGH, name)
        # 读 / 检索 / 等待 / 询问，以及"停止任务"（安全方向，不拦）
        for name in ("read_transl_cache", "get_runtime", "wait", "ask_user", "stop_translation"):
            self.assertEqual(_tool_risk(name), PERMISSION_READ, name)
        # 没登记的按 high：新增写类工具忘了登记时宁可多问一次
        self.assertEqual(_tool_risk("some_new_write_tool"), PERMISSION_HIGH)

    def test_every_tool_has_a_risk_class(self) -> None:
        """工具表里的每个工具都得显式归过类：新增工具忘了登记，这里会红。"""
        declared = {tool["function"]["name"] for tool in AGENT_TOOLS}
        self.assertEqual(declared - (PERMISSION_READ_TOOLS | set(PERMISSION_TOOL_RISK)), set())

    def test_mode_normalization(self) -> None:
        for mode in PERMISSION_MODES:
            self.assertEqual(_normalize_permission_mode(mode), mode)
        # 首尾空白容忍
        self.assertEqual(_normalize_permission_mode(" auto "), "auto")
        # 空值 / 乱七八糟的值一律回落到默认档，不静默放宽权限
        for bad in ("", None, "yes", "Auto", 3):
            self.assertEqual(_normalize_permission_mode(bad), DEFAULT_PERMISSION_MODE)
        self.assertEqual(DEFAULT_PERMISSION_MODE, "ask")

    def test_denied_reason_distinguishes_cause(self) -> None:
        self.assertIn("用户拒绝权限", _permission_denied_reason("save_dict", "deny"))
        self.assertIn("没有在", _permission_denied_reason("save_dict", "timeout"))
        self.assertIn("回合被停止", _permission_denied_reason("save_dict", "stopped"))
        self.assertIn("保存字典", _permission_denied_reason("save_dict", "deny"))  # 用人话的工具名


class PermissionGateTests(unittest.TestCase):
    """门禁本身：什么时候不拦、什么时候挂起等答复、答复之后会怎样。"""

    # ---- 不拦的情况 ----

    def test_auto_never_asks(self) -> None:
        runner = make_runner("auto")
        for name in ("save_dict", "patch_transl_cache", "update_project_config", "start_translation"):
            runner._require_permission(name, {})  # 直接返回即通过（阻塞的话测试会挂住）
        self.assertEqual([e.type for e in runner.state.events], [])  # 也没发审批事件

    def test_accept_edits_allows_edits_without_asking(self) -> None:
        runner = make_runner("accept-edits")
        for name in ("save_dict", "patch_transl_cache", "delete_transl_cache", "save_name_table"):
            runner._require_permission(name, {})
        self.assertEqual([e.type for e in runner.state.events], [])

    def test_read_tools_never_ask_in_any_mode(self) -> None:
        for mode in PERMISSION_MODES:
            runner = make_runner(mode)
            runner._require_permission("read_transl_cache", {})
            runner._require_permission("stop_translation", {})
            self.assertEqual([e.type for e in runner.state.events], [], mode)

    def test_session_grant_skips_the_card(self) -> None:
        runner = make_runner("ask", grants=("save_dict",))
        runner._require_permission("save_dict", {})  # 本会话已放行 → 直接跑
        self.assertEqual([e.type for e in runner.state.events], [])

    # ---- 挂起与答复 ----

    def test_emits_request_event_with_tool_identity(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "patch_transl_cache", "allow-once")

        self.assertTrue(out["ok"], out.get("error"))
        events = [e for e in runner.state.events if e.type == "permission_request"]
        self.assertEqual(len(events), 1)
        data = events[0].data
        self.assertEqual(data["name"], "patch_transl_cache")
        self.assertEqual(data["label"], "修改译文")
        self.assertEqual(data["risk"], PERMISSION_EDIT)
        self.assertEqual(data["mode"], "ask")
        self.assertEqual(data["arguments"]["filename"], "a.json")
        # 模型填的 reason 跟着 arguments 原样进审批卡：用户在批之前能看到"为什么要做这件事"
        # （启动翻译、改配置这类动作全靠它）
        self.assertEqual(data["arguments"]["reason"], "试试")
        self.assertEqual(data["timeout_s"], int(rt.PERMISSION_TIMEOUT))
        # 起始时刻：界面倒计时按它算，刷新后也接着真实剩余时间走（不是从满值重来）
        self.assertLessEqual(abs(time.time() - float(data["started_at"])), 30)

    def test_allow_once_does_not_grant_the_session(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", "allow-once")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(runner.state.permission_grants, set())

    def test_allow_session_grants_that_tool_only(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", "allow-session")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(runner.state.permission_grants, {"save_dict"})
        # 同一个工具再调一次：不再打扰用户
        runner._require_permission("save_dict", {})
        self.assertEqual(len([e for e in runner.state.events if e.type == "permission_request"]), 1)
        # 别的工具仍然要问（本会话放行是按工具名生效的）
        out2 = run_gate(runner, "patch_transl_cache", "deny")
        self.assertIsInstance(out2["error"], AgentToolError)

    def test_deny_raises_tool_error_for_the_model(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "update_project_config", "deny")

        self.assertIsInstance(out["error"], AgentToolError)
        self.assertIn("用户拒绝权限", str(out["error"]))
        self.assertEqual(runner.state.permission_grants, set())

    def test_high_risk_asks_under_accept_edits(self) -> None:
        runner = make_runner("accept-edits")
        out = run_gate(runner, "write_project_guideline", "allow-once")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(
            [e.data["name"] for e in runner.state.events if e.type == "permission_request"],
            ["write_project_guideline"],
        )

    def test_timeout_is_denied(self) -> None:
        runner = make_runner("ask")
        with patch.object(rt, "PERMISSION_TIMEOUT", 0.2):
            with self.assertRaises(AgentToolError) as ctx:
                runner._require_permission("save_dict", {})
        self.assertIn("没有在", str(ctx.exception))

    def test_stop_event_denies(self) -> None:
        runner = make_runner("ask")
        runner.stop_event.set()
        with self.assertRaises(AgentToolError) as ctx:
            runner._require_permission("save_dict", {})
        self.assertIn("回合被停止", str(ctx.exception))

    # ---- resolve_permission 的校验 ----

    def test_resolve_without_pending_request(self) -> None:
        runner = make_runner("ask")
        with self.assertRaises(ValueError):
            runner.resolve_permission("allow-once")

    def test_resolve_rejects_unknown_decision_and_keeps_the_request_pending(self) -> None:
        runner = make_runner("ask")
        run_gate(runner, "save_dict", None)  # 只等挂起，不答
        with self.assertRaises(ValueError):
            runner.resolve_permission("yolo")  # 不认识的值直接报错
        # 请求仍在（下面这句能答上就说明"yolo"没有把它清掉），答完线程收尾
        runner.resolve_permission("deny")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with runner._perm_lock:
                if runner._pending_permission is None:
                    break
            time.sleep(0.01)
        with runner._perm_lock:
            self.assertIsNone(runner._pending_permission)


class AnswerPermissionTests(unittest.TestCase):
    """HTTP 层入口：找不到在等待的会话就报 ValueError（界面按 409 提示）。"""

    # 用一个绝不会存在会话的项目目录（否则 _resolve_session_id 会挑到别的会话）
    NO_SESSION_PROJECT = r"C:\__no_such_galtransl_project__"

    def test_no_waiting_session(self) -> None:
        registry = AgentRuntime()
        with self.assertRaises(ValueError):
            registry.answer_permission(self.NO_SESSION_PROJECT, None, "allow-once")

    def test_set_mode_without_session(self) -> None:
        registry = AgentRuntime()
        with self.assertRaises(ValueError):
            registry.set_permission_mode(self.NO_SESSION_PROJECT, None, "auto")


class PermissionModeSwitchClearsGrantsTests(unittest.TestCase):
    """换档即清空「本会话允许」：档位是信任级别，旧档下点过的不该跨档沿用。

    反面同样重要：**同档重复设置不算换档**——前端每回合都会带上当前档位，
    要是按"设置过就清"处理，「本会话允许」连一个回合都活不过去。
    """

    def test_switch_clears_session_grants(self) -> None:
        runner = make_runner("ask", grants=("save_dict", "patch_transl_cache"))

        runner.apply_permission_mode("accept-edits")

        self.assertEqual(runner.state.permission_mode, "accept-edits")
        self.assertEqual(runner.state.permission_grants, set())

    def test_switching_back_does_not_restore_old_grants(self) -> None:
        runner = make_runner("ask", grants=("save_dict",))
        runner.apply_permission_mode("auto")
        runner.state.permission_grants.add("update_project_config")  # 新档下重新点的

        runner.apply_permission_mode("ask")

        self.assertEqual(runner.state.permission_grants, set())

    def test_same_mode_keeps_grants(self) -> None:
        runner = make_runner("ask", grants=("save_dict",))

        runner.apply_permission_mode("ask")  # 前端每次 start/message 都会带当前档
        runner.apply_permission_mode("")  # 空值/乱值回落到默认档 = 没换档
        runner.apply_permission_mode("nonsense")

        self.assertEqual(runner.state.permission_grants, {"save_dict"})

    def test_runtime_switch_clears_grants_without_a_runner(self) -> None:
        registry = AgentRuntime()
        state = AgentState(project_dir=r"C:\proj", session_id="s1", permission_mode="ask")
        state.permission_grants.add("save_dict")
        registry._states.setdefault(registry._key(r"C:\proj"), {})["s1"] = state

        out = registry.set_permission_mode(r"C:\proj", "s1", "auto")

        self.assertEqual(out["permission_mode"], "auto")
        self.assertEqual(state.permission_grants, set())

    def test_message_with_a_new_mode_clears_grants(self) -> None:
        """另一条改档路径：前端随消息送过来的档位变了，同样清空放行。"""
        registry = AgentRuntime()
        state = AgentState(
            project_dir=r"C:\proj",
            session_id="s1",
            status="running",  # 运行中：消息只进队列，不会真起回合
            permission_mode="ask",
        )
        state.messages.append({"role": "user", "content": "hi"})
        state.permission_grants.add("save_dict")
        registry._states.setdefault(registry._key(r"C:\proj"), {})["s1"] = state

        registry.message(r"C:\proj", "再改一处", "s1", permission_mode="accept-edits")

        self.assertEqual(state.permission_mode, "accept-edits")
        self.assertEqual(state.permission_grants, set())
        self.assertEqual(len(state.pending_messages), 1)  # 确实走了排队那条分支


class LivePermissionModeTests(unittest.TestCase):
    """跑着也能改档：改完下一次判定就用新档；在等的那张卡若已无必要会自己放行。

    改档只能改前端那一份的话，用户在"卡弹出来之后"才切全自动就没用了——他得先回答
    那张已经不合时宜的卡。所以切到更宽的档时，后端把在等的请求按「允许一次」放行。
    """

    def test_switching_to_a_wider_mode_releases_the_pending_card(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", None)  # 挂起，先不点

        runner.apply_permission_mode("accept-edits")  # 这一档本来就会放行缓存/字典写

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not out.get("ok") and "error" not in out:
            time.sleep(0.01)
        self.assertTrue(out.get("ok"), out.get("error"))
        self.assertEqual(runner.state.permission_mode, "accept-edits")
        # 只是把这一次放过去，不写「本会话放行」——那是用户点按钮才有的语义
        self.assertEqual(runner.state.permission_grants, set())

    def test_next_call_after_the_switch_uses_the_new_mode(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", None)
        runner.apply_permission_mode("auto")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not out.get("ok") and "error" not in out:
            time.sleep(0.01)
        # 全自动档下，连高风险工具也不再问（也不该再发审批事件）
        runner._require_permission("update_project_config", {})
        self.assertEqual(len([e for e in runner.state.events if e.type == "permission_request"]), 1)

    def test_switching_to_a_stricter_mode_keeps_waiting(self) -> None:
        runner = make_runner("accept-edits")
        out = run_gate(runner, "write_project_guideline", None)  # 高风险：挂着
        runner.apply_permission_mode("ask")  # 更严，仍然该问

        time.sleep(0.05)
        with runner._perm_lock:
            self.assertIsNotNone(runner._pending_permission)
            self.assertEqual(runner._pending_permission["decision"], "")

        runner.resolve_permission("deny")  # 收拾掉，别让线程悬着
        # 等子线程真的收尾（挂起槽清空 ≠ 抛错已经写进 out，中间还差几微秒）
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and "error" not in out and not out.get("ok"):
            time.sleep(0.01)
        self.assertIsInstance(out.get("error"), AgentToolError)


class PermissionDispatchTests(unittest.TestCase):
    """门禁挂在 _dispatch_tool 上：被拒绝时 handler 根本不执行。"""

    def test_denied_tool_never_runs_but_allowed_one_does(self) -> None:
        runner = make_runner("ask")
        called: list[str] = []

        def fake_handler(_runner, _args):
            called.append("ran")
            return {"ok": True}

        with patch.dict(rt._TOOL_HANDLERS, {"patch_transl_cache": fake_handler}):
            out = run_gate(runner, "patch_transl_cache", "deny", dispatch=True)
            self.assertIsInstance(out["error"], AgentToolError)
            self.assertEqual(called, [])

            out2 = run_gate(runner, "patch_transl_cache", "allow-once", dispatch=True)
            self.assertTrue(out2["ok"], out2.get("error"))
            self.assertEqual(called, ["ran"])


if __name__ == "__main__":
    unittest.main()
