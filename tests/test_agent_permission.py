"""Agent 的权限门禁：模式 × 风险矩阵、审批阻塞、三种答复的后果。

对照参考实现（PI-Desktop 的 permission mode）：
- 四档模式 ask / accept-edits / auto / auto-quiet（全自动-零打断），默认 ask；
- 工具分 read / edit / high 三档风险，**没登记的按 high**（fail closed）；
- ask：写操作一律先问；accept-edits：只自动放行"改译文数据"（缓存/字典/人名表）；
  auto：全放行；auto-quiet 放行规则同 auto，另把 ask_user 改成按推荐项代答（不打断用户）；
- 答复只有 allow-once / allow-session（按工具名，本会话有效）/ deny；
- **不设超时**：没人答就一直挂着（与 ask_user 一致），只有回合被停止才按拒绝收尾；
  拒绝时给模型一条"用户拒绝权限"的工具错误（可附用户填的拒绝原因）。
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    AUTO_QUIET_MODE,
    DEFAULT_PERMISSION_MODE,
    PERMISSION_EDIT,
    PERMISSION_HIGH,
    PERMISSION_MODE_LABELS,
    PERMISSION_MODES,
    PERMISSION_READ,
    PERMISSION_READ_TOOLS,
    PERMISSION_TOOL_RISK,
    AgentRunner,
    AgentRuntime,
    AgentState,
    AgentToolError,
    _build_system_prompt,
    _normalize_permission_mode,
    _normalize_permission_reason,
    _permission_denied_reason,
    _permission_needed,
    _tool_risk,
)


def make_runner(mode: str = "ask", grants: tuple[str, ...] = ()) -> AgentRunner:
    # 不给 session_id：AgentRunner 就不建 SessionStore，测试不会往磁盘写会话文件
    state = AgentState(project_dir=r"C:\proj", permission_mode=mode)
    state.permission_grants.update(grants)
    return AgentRunner(state)


def run_gate(
    runner: AgentRunner,
    name: str,
    decision: str | None,
    *,
    dispatch: bool = False,
    reason: str | None = None,
    args: dict | None = None,
) -> dict:
    """子线程里跑一次门禁（或整条 _dispatch_tool），等它挂起后作答。

    decision=None 表示只等挂起、不作答（用于测校验分支）。reason 是"拒绝原因"，
    跟着答复一起送（见 resolve_permission）。args 换掉默认入参（要触发预览的用例
    得给全 patches / content 这些）。返回 {ok/error/alive}。
    """
    out: dict = {}
    call_args = args if args is not None else {"filename": "a.json", "reason": "试试"}

    def target() -> None:
        try:
            if dispatch:
                runner._dispatch_tool(name, call_args)
            else:
                runner._require_permission(name, call_args)
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
        runner.resolve_permission(decision, reason or "")
    # 作答了才值得等它跑完；只等挂起的那些（decision=None）别把 3 秒白等掉
    thread.join(timeout=3 if decision is not None else 0.05)
    out["alive"] = thread.is_alive()
    return out


class NoModeInSystemPromptTests(unittest.TestCase):
    """档位一律不写进 system prompt。

    permission_mode 可以随时切，写进 system 就等于把整段前缀缓存钉死在某个档位上；
    「全自动-零打断」的"不打断"改由后端代答 ask_user 实现（见 test_agent_ask_user 的
    AutoQuietAutoAnswerTests），跟提示词无关。这里钉住"任何档位都不注入档位说明"。
    """

    def _prompt(self, mode: str) -> str:
        state = AgentState(project_dir=r"C:\proj", permission_mode=mode)
        return _build_system_prompt(state)

    def test_no_mode_is_mentioned_in_the_prompt(self) -> None:
        for mode in PERMISSION_MODES:
            prompt = self._prompt(mode)
            for fragment in ("请提高自主性", "全自动-零打断", "全自动-减少问询"):
                self.assertNotIn(fragment, prompt, f"{mode} 的 system prompt 不该出现 {fragment}")

    def test_every_mode_yields_the_same_prompt(self) -> None:
        """字节级一致：切档位不会让前缀失效，缓存能一直命中。"""
        self.assertEqual(len({self._prompt(mode) for mode in PERMISSION_MODES}), 1)

    def test_switching_mode_keeps_the_prompt_stable(self) -> None:
        state = AgentState(project_dir=r"C:\proj", permission_mode="auto")
        before = _build_system_prompt(state)

        rt._apply_permission_mode(state, AUTO_QUIET_MODE)

        self.assertEqual(_build_system_prompt(state), before)


class PermissionMatrixTests(unittest.TestCase):
    def test_matrix(self) -> None:
        """读永远放行；编辑在 ask 要问、其余档放行；高风险只有两个全自动档放行。"""
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
            # 「全自动-零打断」的放行规则与「全自动」完全一致（差别在 ask_user 是否代答）
            (PERMISSION_READ, AUTO_QUIET_MODE): False,
            (PERMISSION_EDIT, AUTO_QUIET_MODE): False,
            (PERMISSION_HIGH, AUTO_QUIET_MODE): False,
        }
        for (risk, mode), expected in cases.items():
            self.assertEqual(_permission_needed(risk, mode), expected, f"{risk}/{mode}")

    def test_every_mode_has_a_label(self) -> None:
        """档位与菜单文案一一对应：加了档位忘了写标签，这里会红。"""
        self.assertEqual(set(PERMISSION_MODE_LABELS), set(PERMISSION_MODES))

    def test_risk_classification(self) -> None:
        # 改译文数据：缓存 / 字典 / 人名表 —— "允许编辑"档放行的就是这些
        for name in ("patch_transl_cache", "delete_transl_cache", "save_dict", "create_dict_file", "save_name_table"):
            self.assertEqual(_tool_risk(name), PERMISSION_EDIT, name)
        # 改设置 / 规范、启动任务、派子代理：只有全自动放行（「允许编辑」档也要问）
        for name in (
            "update_project_config",
            "manage_problem_filter",
            "write_project_guideline",
            "start_translation",
            "run_subagents",
        ):
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
        self.assertIn("回合被停止", _permission_denied_reason("save_dict", "stopped"))
        self.assertIn("保存字典", _permission_denied_reason("save_dict", "deny"))  # 用人话的工具名

    def test_denied_reason_carries_the_note_from_the_user(self) -> None:
        """用户填的拒绝原因原样进那句话；他没填、或不是拒绝（回合被停）时不该冒出来。"""
        with_note = _permission_denied_reason("save_dict", "deny", "这本字典我自己维护")
        self.assertIn("用户填写的拒绝原因：这本字典我自己维护", with_note)

        self.assertNotIn("原因", _permission_denied_reason("save_dict", "deny"))
        self.assertNotIn("不该出现", _permission_denied_reason("save_dict", "stopped", "不该出现"))

    def test_reason_normalization(self) -> None:
        self.assertEqual(_normalize_permission_reason(None), "")
        self.assertEqual(_normalize_permission_reason(123), "")
        self.assertEqual(_normalize_permission_reason("  a\n b  "), "a b")  # 折行压成空格
        long_text = "字" * (rt.PERMISSION_REASON_MAX + 50)
        clipped = _normalize_permission_reason(long_text)
        self.assertEqual(len(clipped), rt.PERMISSION_REASON_MAX)
        self.assertTrue(clipped.endswith("…"))


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
        # 不设超时，所以事件里不带任何"还剩多少"的字段：界面那张卡没有倒计时，
        # 也不会到点自己收尾（老会话事件里可能还带着，界面已不读）
        self.assertNotIn("timeout_s", data)
        self.assertNotIn("started_at", data)

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

    def test_deny_reason_reaches_the_model(self) -> None:
        """卡上填的拒绝原因随工具结果回给模型：它会出现在那次调用的结果里。"""
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", "deny", reason="这本字典我自己维护，别动")

        err = str(out["error"])
        self.assertIn("用户拒绝权限", err)
        self.assertIn("用户填写的拒绝原因：这本字典我自己维护，别动", err)

    def test_deny_reason_is_cleaned_up(self) -> None:
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", "deny", reason="  先说不要\n\n再说原因  ")
        self.assertIn("先说不要 再说原因", str(out["error"]))

    def test_reason_is_ignored_when_allowing(self) -> None:
        """批准时填的原因没意义：不拦放行、不进结果、也不落任何状态。"""
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", "allow-once", reason="顺手写点什么")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(runner.state.permission_grants, set())

    def test_no_answer_keeps_waiting(self) -> None:
        """不设超时：没人答就一直挂着，不自己替用户作决定（以前 120 秒到点自动拒绝）。"""
        runner = make_runner("ask")
        out = run_gate(runner, "save_dict", None)  # 只等挂起，不作答

        time.sleep(0.5)
        self.assertTrue(out["alive"])  # 回合线程仍挂在这次审批上
        with runner._perm_lock:
            self.assertIsNotNone(runner._pending_permission)
            self.assertEqual(runner._pending_permission["decision"], "")

        runner.resolve_permission("deny")  # 收拾掉：作答之后它才继续
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and "error" not in out:
            time.sleep(0.01)
        self.assertIsInstance(out["error"], AgentToolError)
        self.assertIn("用户拒绝权限", str(out["error"]))

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

    def test_registry_forwards_the_reason_to_the_runner(self) -> None:
        """HTTP 层把 reason 一路送到 runner（不然界面上填了也白填）。"""
        registry = AgentRuntime()
        received: list[tuple[str, str]] = []
        fake = SimpleNamespace(
            resolve_permission=lambda decision, reason="": (
                received.append((decision, reason)) or {"ok": True}
            )
        )
        key = registry._key(r"C:\proj")
        registry._states.setdefault(key, {})["s1"] = AgentState(
            project_dir=r"C:\proj", session_id="s1"
        )
        registry._runners.setdefault(key, {})["s1"] = fake

        out = registry.answer_permission(r"C:\proj", "s1", "deny", "别动字典")

        self.assertEqual(received, [("deny", "别动字典")])
        self.assertEqual(out, {"ok": True})

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


class _PreviewRunner:
    """预览用的只读替身：给 `_http_get` 备好缓存/字典/人名表/配置/规范，并记录有没有人写过。

    预览必须只读——`_http_post` 与不带 dry_run 的 `_http_put` 只会被记下来（测试断言它们
    一直是空的），落盘与否由获批后的 handler 决定。唯一的例外是项目规范：预览也走那个
    PUT 入口，靠服务端 dry_run 保证不落盘，所以这里单独记 dry_runs（并回一份预设的
    "会写成什么样"）。
    """

    def __init__(self, *, cache=None, dict_contents=None, names=None, config=None, guideline="", guideline_after=""):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir=r"C:\proj")
        self.cache = cache or {}
        self.dict_contents = dict_contents or {}
        self.names = names or []
        self.config = config if config is not None else {}
        self.guideline = guideline
        # 真服务端由 apply_project_guideline_edit(dry_run) 算出这份文本，替身里直接给
        self.guideline_after = guideline_after
        self.writes: list[str] = []
        self.dry_runs: list[str] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, url: str):
        if url.endswith("/name-table"):
            return {"names": self.names}
        if url.endswith("/guideline"):
            return {"content": self.guideline}
        if "/dictionary/project" in url:
            return {"dict_contents": self.dict_contents}
        if "?config=" in url:  # /config?config=...（字典那条也带 config 查询，所以放它后面）
            return {"config": self.config}
        for name, entries in self.cache.items():
            if url.endswith(f"/cache/{name}"):
                return {"entries": [dict(e) for e in entries]}
        raise AssertionError(f"预览不该读这个地址：{url}")

    def _http_post(self, url: str, _body):
        self.writes.append(url)
        return {}

    def _http_put(self, url: str, body):
        if body.get("dry_run"):
            self.dry_runs.append(url)
            return {"dry_run": True, "content": self.guideline_after}
        self.writes.append(url)
        return {}


class PermissionPreviewTests(unittest.TestCase):
    """审批卡的「将要变更」：挂起前只读算出 before→after，且绝不落盘。

    预览与真执行**共用同一批计划函数**（_plan_cache_patches / _plan_cache_delete /
    _dict_new_lines / _name_table_changes），所以这里钉的是"两边口径完全一致"，
    而不是另写一份 diff 算法去对答案。
    """

    # ---- 缓存：patch ----

    def test_patch_preview_shows_before_after_without_writing(self) -> None:
        runner = _PreviewRunner(cache={"a.json": [{"index": 33, "pre_dst": "旧译文"}]})

        preview = rt._preview_tool_changes(
            runner, "patch_transl_cache", {"filename": "a.json", "patches": [{"index": 33, "pre_dst": "新译文"}]}
        )

        self.assertEqual(preview["filename"], "a.json")
        self.assertEqual(preview["changes"][0]["path"], "#33.pre_dst")
        self.assertEqual(preview["changes"][0]["before"], "旧译文")
        self.assertEqual(preview["changes"][0]["after"], "新译文")
        self.assertEqual(runner.writes, [])  # 只读：一个字都没写

    def test_patch_preview_matches_what_the_real_run_writes(self) -> None:
        """同一条 index 被两条 patch 命中时，第二条的 before 也要是第一条的 after。"""
        args = {
            "filename": "a.json",
            "patches": [{"index": 7, "pre_dst": "B"}, {"index": 7, "pre_dst": "C"}],
        }
        preview = rt._preview_tool_changes(
            _PreviewRunner(cache={"a.json": [{"index": 7, "pre_dst": "A"}]}), "patch_transl_cache", args
        )
        real = rt._tool_patch_transl_cache(
            _PreviewRunner(cache={"a.json": [{"index": 7, "pre_dst": "A"}]}), args
        )

        self.assertEqual([c["before"] for c in preview["changes"]], ["A", "B"])
        self.assertEqual([c["after"] for c in preview["changes"]], ["B", "C"])
        self.assertEqual(preview["changes"], real["changes"])

    def test_patch_preview_skipped_when_nothing_would_change(self) -> None:
        runner = _PreviewRunner(cache={"a.json": [{"index": 1, "pre_dst": "x"}]})
        # index 不存在 / 字段不在白名单 / 工具没给 patches：都没有 diff 可显示
        for args in (
            {"filename": "a.json", "patches": [{"index": 99, "pre_dst": "y"}]},
            {"filename": "a.json", "patches": [{"index": 1, "trans_by": "我"}]},
            {"filename": "a.json"},
        ):
            self.assertIsNone(rt._preview_tool_changes(runner, "patch_transl_cache", args), args)

    # ---- 缓存：按 index 删除 ----

    def test_delete_preview_lists_the_entries_that_go_away(self) -> None:
        runner = _PreviewRunner(
            cache={"a.json": [{"index": 1, "pre_dst": "留下"}, {"index": 2, "pre_dst": "删掉我"}]}
        )

        preview = rt._preview_tool_changes(
            runner, "delete_transl_cache", {"filename": "a.json", "indexes": "2"}
        )

        self.assertEqual(preview["deleted_indexes"], [2])
        self.assertEqual(preview["deleted_preview"], [{"index": 2, "text": "删掉我"}])
        self.assertEqual(runner.writes, [])

    def test_delete_preview_absent_for_whole_file_delete(self) -> None:
        runner = _PreviewRunner(cache={"a.json": [{"index": 1, "pre_dst": "x"}]})
        # 整文件删除没有可比对的 diff：卡上退回摘要，也不去读缓存
        self.assertIsNone(
            rt._preview_tool_changes(runner, "delete_transl_cache", {"filename": "a.json"})
        )

    # ---- 字典 ----

    def test_dict_preview_diffs_lines_and_matches_the_real_run(self) -> None:
        before = ["旧词\told", "重复\tdup"]
        args = {"file_key": "pre.json", "action": "append", "content": "重复\tdup\n新词\tnew"}
        contents = {"pre.json": {"lines": before}}

        preview = rt._preview_tool_changes(_PreviewRunner(dict_contents=contents), "save_dict", args)
        real = rt._tool_save_dict(_PreviewRunner(dict_contents=contents), args)

        self.assertEqual([r["op"] for r in preview["line_diff"]["rows"]], ["add"])
        self.assertEqual(preview["line_diff"], real["line_diff"])
        # 已存在的 key 不重复追加（append 的合并规则与真执行同一份）
        self.assertIn("新词\tnew", preview["line_diff"]["rows"][0]["line"])

    def test_dict_preview_absent_when_content_is_unchanged(self) -> None:
        runner = _PreviewRunner(dict_contents={"pre.json": {"lines": ["同\tsame"]}})
        self.assertIsNone(
            rt._preview_tool_changes(
                runner, "save_dict", {"file_key": "pre.json", "action": "overwrite", "content": "同\tsame"}
            )
        )

    # ---- 人名表 ----

    def test_name_table_preview_lists_added_removed_and_renamed(self) -> None:
        old = [
            {"src_name": "ウェアウルフ", "dst_name": "狼人", "count": 2},
            {"src_name": "ゴブリン", "dst_name": "哥布林", "count": 2},
        ]
        new = [
            {"src_name": "ウェアウルフ", "dst_name": "狼人族", "count": 2},  # 改译名
            {"src_name": "ゴブリン", "dst_name": "哥布林", "count": 2},  # 原样回传
            {"src_name": "ドラゴン", "dst_name": "龙", "count": 1},  # 新增
        ]
        runner = _PreviewRunner(names=old)

        preview = rt._preview_tool_changes(runner, "save_name_table", {"names": new})

        # 路径是 src_name、值是 dst_name：改译名是 replace，不是"加一条删一条"
        self.assertEqual(
            [(c["path"], c["kind"], c["before"], c["after"]) for c in preview["changes"]],
            [("ドラゴン", "add", None, "龙"), ("ウェアウルフ", "replace", "狼人", "狼人族")],
        )
        self.assertEqual(runner.writes, [])

    def test_name_table_preview_matches_the_real_run(self) -> None:
        old = [{"src_name": "A", "dst_name": "甲", "count": 1}]
        new = [
            {"src_name": "A", "dst_name": "甲2", "count": 1},
            {"src_name": "B", "dst_name": "", "count": 0},  # 译名还空着
        ]
        args = {"names": new}

        preview = rt._preview_tool_changes(_PreviewRunner(names=old), "save_name_table", args)
        real = rt._tool_save_name_table(_PreviewRunner(names=old), args)

        self.assertEqual(preview["changes"], real["changes"])
        self.assertEqual(real["names_added"], ["B"])
        self.assertEqual(real["names_removed"], [])

    def test_name_table_preview_lists_removed_names_with_their_old_dst(self) -> None:
        old = [{"src_name": "A", "dst_name": "甲", "count": 1}]

        preview = rt._preview_tool_changes(_PreviewRunner(names=old), "save_name_table", {"names": []})

        self.assertEqual(
            [(c["path"], c["kind"], c["before"], c["after"]) for c in preview["changes"]],
            [("A", "remove", "甲", None)],
        )

    def test_name_table_preview_is_quiet_when_only_counts_change(self) -> None:
        """回归：原样回传同一张表（只有 count 动过）不该冒出「+ {'src_name': ...}」「− None」那种行。

        接口的 entry 是 {src_name, dst_name, count}，早先这里拿整条 entry 去比对、旧值还读错了
        字段（读的是不存在的 name），于是"保存"这样一张没改过的表会显示成一堆新增 + 一行 None。
        """
        old = [{"src_name": "ゴブリン", "dst_name": "哥布林", "count": 2}]
        new = [{"src_name": "ゴブリン", "dst_name": "哥布林", "count": 9}]

        self.assertIsNone(
            rt._preview_tool_changes(_PreviewRunner(names=old), "save_name_table", {"names": new})
        )

    # ---- 项目配置（high 档也要提前看 diff） ----

    def test_config_preview_shows_key_before_after_and_matches_the_real_run(self) -> None:
        config = {"common": {"gpt": {"contextNum": 8}}}
        args = {"updates": [{"key": "common.gpt.contextNum", "value": 12}]}

        preview = rt._preview_tool_changes(_PreviewRunner(config=config), "update_project_config", args)
        real = rt._tool_update_project_config(_PreviewRunner(config=config), args)

        self.assertEqual(
            [(c["path"], c["before"], c["after"]) for c in preview["changes"]],
            [("common.gpt.contextNum", 8, 12)],
        )
        self.assertEqual(preview["changes"], real["changes"])

    def test_config_preview_chains_two_updates_on_the_same_key(self) -> None:
        """同一个键改两次：第二条的 before 要是第一条的 after（真执行是逐条写下去的）。"""
        config = {"common": {"gpt": {"contextNum": 8}}}
        args = {
            "updates": [
                {"key": "common.gpt.contextNum", "value": 12},
                {"key": "common.gpt.contextNum", "value": 16},
            ]
        }

        preview = rt._preview_tool_changes(_PreviewRunner(config=config), "update_project_config", args)

        self.assertEqual([(c["before"], c["after"]) for c in preview["changes"]], [(8, 12), (12, 16)])

    def test_config_preview_absent_when_no_key_would_change(self) -> None:
        config = {"common": {"gpt": {"contextNum": 8}}}
        runner = _PreviewRunner(config=config)
        for updates in (
            [{"key": "common.gpt.madeUpKey", "value": 1}],  # 配置里不存在
            [],  # 空 updates
        ):
            self.assertIsNone(
                rt._preview_tool_changes(runner, "update_project_config", {"updates": updates}), updates
            )

    # ---- 问题过滤 ----

    def test_problem_filter_preview_lists_added_keys_and_matches_the_real_run(self) -> None:
        config = {"common": {"problemFilterKey": ["残留日文"]}}
        args = {"action": "add", "keyword": ["残留日文", "标点错漏"]}

        preview = rt._preview_tool_changes(_PreviewRunner(config=config), "manage_problem_filter", args)
        real = rt._tool_manage_problem_filter(_PreviewRunner(config=config), args)

        # 已在清单里的那个不算改动，卡上只列真正会加进去的
        self.assertEqual([(c["kind"], c["after"]) for c in preview["changes"]], [("add", "标点错漏")])
        self.assertEqual(preview["changes"], real["changes"])

    def test_problem_filter_preview_lists_removed_keys(self) -> None:
        config = {"common": {"problemFilterKey": ["残留日文", "标点错漏"]}}

        preview = rt._preview_tool_changes(
            _PreviewRunner(config=config), "manage_problem_filter", {"action": "remove", "keyword": "标点错漏"}
        )

        self.assertEqual([(c["kind"], c["before"]) for c in preview["changes"]], [("remove", "标点错漏")])

    def test_problem_filter_preview_absent_when_nothing_would_change(self) -> None:
        runner = _PreviewRunner(config={"common": {"problemFilterKey": ["残留日文"]}})
        for args in (
            {"action": "list"},  # 查清单不改东西
            {"action": "add", "keyword": "残留日文"},  # 本来就有
            {"action": "remove", "keyword": "不在清单里"},  # 本来就没有
            {"action": "add"},  # 没给 keyword（真执行会报错，卡上不必先闪一下）
        ):
            self.assertIsNone(rt._preview_tool_changes(runner, "manage_problem_filter", args), args)

    # ---- 项目规范（dry_run 走服务端同一份拼接逻辑） ----

    def test_guideline_preview_diffs_what_the_server_would_write(self) -> None:
        runner = _PreviewRunner(guideline="原有\n", guideline_after="原有\n新增\n")

        preview = rt._preview_tool_changes(
            runner, "write_project_guideline", {"mode": "append", "content": "新增"}
        )

        self.assertEqual(
            [(r["op"], r["line"]) for r in preview["line_diff"]["rows"]],
            [("add", "新增")],
        )
        self.assertEqual(runner.dry_runs, ["/api/projects/proj/guideline"])  # 走的是 dry_run 那一发
        self.assertEqual(runner.writes, [])  # 真写没有发生

    def test_guideline_preview_absent_when_content_is_unchanged(self) -> None:
        runner = _PreviewRunner(guideline="同样的一段", guideline_after="同样的一段")
        self.assertIsNone(
            rt._preview_tool_changes(runner, "write_project_guideline", {"mode": "append", "content": "x"})
        )

    def test_guideline_preview_absent_for_unknown_mode(self) -> None:
        runner = _PreviewRunner(guideline="a", guideline_after="b")
        # 模式不认识就不去问后端（真执行会拿到 400，卡片没必要先闪一下）
        self.assertIsNone(rt._preview_tool_changes(runner, "write_project_guideline", {"mode": "prepend"}))
        self.assertEqual(runner.dry_runs, [])

    # ---- 不适用 / 取数失败 ----

    def test_tools_without_a_diff_have_no_preview(self) -> None:
        runner = _PreviewRunner()
        for name in ("start_translation", "run_subagents", "create_dict_file"):
            # 连读都不读：不在 PREVIEW_TOOLS 里就直接返回（_PreviewRunner 会对意外读操作报错）
            self.assertIsNone(rt._preview_tool_changes(runner, name, {"filename": "a.json"}), name)

    def test_read_failure_never_raises_and_yields_no_preview(self) -> None:
        def boom(_url: str):
            raise RuntimeError("后端不可达")

        runner = _PreviewRunner()
        runner._http_get = boom

        self.assertIsNone(
            rt._preview_tool_changes(
                runner,
                "patch_transl_cache",
                {"filename": "a.json", "patches": [{"index": 1, "pre_dst": "x"}]},
            )
        )

    # ---- 与门禁接起来 ----

    def test_gate_emits_the_preview_with_the_request(self) -> None:
        runner = make_runner("ask")
        runner._http_get = lambda _url: {"entries": [{"index": 33, "pre_dst": "旧译文"}]}

        out = run_gate(
            runner,
            "patch_transl_cache",
            "allow-once",
            args={"filename": "a.json", "patches": [{"index": 33, "pre_dst": "新译文"}]},
        )

        self.assertTrue(out["ok"], out.get("error"))
        data = [e for e in runner.state.events if e.type == "permission_request"][0].data
        self.assertEqual(data["preview"]["changes"][0]["after"], "新译文")

    def test_gate_omits_the_preview_key_when_there_is_nothing_to_show(self) -> None:
        runner = make_runner("ask")

        out = run_gate(runner, "save_dict", "allow-once")

        self.assertTrue(out["ok"], out.get("error"))
        data = [e for e in runner.state.events if e.type == "permission_request"][0].data
        self.assertNotIn("preview", data)  # 空预览不如不给：界面会当成没有


if __name__ == "__main__":
    unittest.main()
