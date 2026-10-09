"""权限：工具执行前的审批（风险分级、权限模式、拒绝原因）。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from GalTransl.Agent.core import _log

if TYPE_CHECKING:
    from GalTransl.Agent.models import AgentState


# ---- 权限（工具执行前的审批）----
# 四档模式，对应输入区那个选择器。审批在**后端**做：模型不知道当前是什么模式
# （也不会被告知），它只会在被拒绝时收到一条工具错误。
# - ask（每次询问，默认）：写操作一律先问；
# - accept-edits（允许编辑）：只自动放行"改译文数据"（缓存 / 字典 / 人名表），
#   改项目配置、改项目规范、启动翻译仍然要问；
# - auto（全自动）：全部放行；
# - auto-quiet（全自动-零打断）：放行规则与 auto 一模一样，差别只有一处——**ask_user
#   不再真的拦下来等人**：后端直接按模型给的「推荐选项」代答（见 _tool_ask_user），
#   用户完全不被打断。其余档位照旧：不把档位写进 system prompt，模型只会在被拒绝时
#   收到一条工具错误。
AUTO_QUIET_MODE = "auto-quiet"
PERMISSION_MODES: tuple[str, ...] = ("ask", "accept-edits", "auto", AUTO_QUIET_MODE)
DEFAULT_PERMISSION_MODE = "ask"
PERMISSION_MODE_LABELS: dict[str, str] = {
    "ask": "每次询问",
    "accept-edits": "允许编辑",
    "auto": "全自动",
    AUTO_QUIET_MODE: "全自动-零打断",
}
# 审批的三种答复（前端按钮）：只批这一次 / 本会话都批这个工具 / 拒绝
PERMISSION_DECISIONS: tuple[str, ...] = ("allow-once", "allow-session", "deny")
# **不设超时**：没人答就一直挂着（与 ask_user 一致），只有回合被停止才算拒绝。
# 以前是 120 秒到点自动拒绝（fail closed），但"离开一会儿回来发现已经被自动拒了、Agent
# 还顺着换了策略"比多等一会儿更糟——审批是该由人决定的事，不该由时钟决定。
# 等待答复的轮询步长（秒）：只为尽快响应停止信号
PERMISSION_WAIT_TICK = 0.2

# 风险分级（只分三档，够上面三种模式用）：
# - read：不改任何东西，任何模式都直接放行；
# - edit：改译文数据（缓存 / 字典 / 人名表）——"允许编辑"档自动放行的就是这些；
# - high：改项目设置 / 项目规范 / 启动任务——只有"全自动"放行。
PERMISSION_READ = "read"
PERMISSION_EDIT = "edit"
PERMISSION_HIGH = "high"

PERMISSION_TOOL_RISK: dict[str, str] = {
    "save_dict": PERMISSION_EDIT,
    "save_name_table": PERMISSION_EDIT,
    "patch_transl_cache": PERMISSION_EDIT,
    "revert_proofread_changes": PERMISSION_EDIT,
    "delete_transl_cache": PERMISSION_EDIT,
    "update_project_config": PERMISSION_HIGH,
    "manage_problem_filter": PERMISSION_HIGH,
    "manage_problem_white_list": PERMISSION_HIGH,
    "write_project_guideline": PERMISSION_HIGH,
    "start_translation": PERMISSION_HIGH,
    # 派子代理：校对默认直接修复任务范围内的译文并反馈需二次审查事项。这是
    # 「要不要开始干这件事」——一次最多 16 个并行跑起来、每个都要调大模型、都会写缓存，
    # 让用户在派之前批一次（卡上能看到派给谁、看哪些文件）比事后发现跑歪了强。所以按
    # high 走：ask 与 accept-edits 都要问，只有两个全自动档直接放行。
    "run_subagents": PERMISSION_HIGH,
}
# 只读类工具：读 / 检索 / 等待 / 询问，外加"停止任务"——停止是安全方向的动作，
# 要停下来还得先点确认是最糟的设计，所以任何模式都直接放行。
PERMISSION_READ_TOOLS: frozenset[str] = frozenset({
    "get_project_overview",
    "get_plugin_settings",
    "list_input_files",
    "read_input_file",
    "search_input",
    "search_output_files",
    "read_guideline",
    "list_dict_files",
    "read_dict",
    "get_name_table",
    "list_problems",
    "read_transl_cache",
    "read_proofread_changes",
    "read_output",
    "read_history_archive",
    "get_runtime",
    "wait",
    "ask_user",
    "stop_translation",
})
# 审批卡上显示的工具名（前端有自己的 TOOL_META，这里只需要一个可读的名字）
PERMISSION_TOOL_LABELS: dict[str, str] = {
    "save_dict": "保存字典",
    "save_name_table": "保存人名表",
    "patch_transl_cache": "修改译文",
    "revert_proofread_changes": "撤销校对修改",
    "delete_transl_cache": "删除缓存",
    "update_project_config": "修改项目配置",
    "manage_problem_filter": "管理问题过滤",
    "manage_problem_white_list": "管理问题白名单",
    "write_project_guideline": "修改项目规范",
    "start_translation": "启动翻译",
    "run_subagents": "派子代理",
}


def _tool_risk(name: str) -> str:
    """工具的风险档。没登记的工具按 high 处理（fail closed）：新增写类工具忘了
    登记时宁可多问一次，也不要默默改掉用户的项目。"""
    if name in PERMISSION_TOOL_RISK:
        return PERMISSION_TOOL_RISK[name]
    if name in PERMISSION_READ_TOOLS:
        return PERMISSION_READ
    return PERMISSION_HIGH


def _permission_tool_label(name: str) -> str:
    return PERMISSION_TOOL_LABELS.get(name, name)


def _normalize_permission_mode(value: Any) -> str:
    """前端送来的模式：不认识的（含空值）一律回落到默认的「每次询问」。"""
    mode = str(value or "").strip()
    return mode if mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def _apply_permission_mode(state: AgentState, mode: Any) -> bool:
    """换档：改掉档位并**清空本会话的放行记录**，返回是否真的换了。

    换档等于换一套信任级别，之前点过的「本会话允许」不再作数——从「每次询问」切到
    「允许编辑」再切回来时，旧放行还留着会让人以为档位没生效。反过来，**同档重复设置
    不算换档**（前端每回合都会带上当前档位），否则「本会话允许」活不过一个回合。
    """
    clean = _normalize_permission_mode(mode)
    if clean == state.permission_mode:
        return False
    state.permission_mode = clean
    if state.permission_grants:
        _log(f"换档为 {clean}，清空 {len(state.permission_grants)} 条本会话放行记录")
        state.permission_grants.clear()
    return True


def _permission_needed(risk: str, mode: str) -> bool:
    """这次调用要不要先请用户批准。读类永远不用；其余按模式矩阵判断。"""
    if risk == PERMISSION_READ:
        return False
    if mode in ("auto", AUTO_QUIET_MODE):
        return False
    if mode == "accept-edits":
        # 只自动放行"改译文数据"，配置/规范/启动任务仍要确认
        return risk != PERMISSION_EDIT
    return True


# 用户填的「拒绝原因」长度上限：一句话够模型换策略了，太长会把工具结果挤成一大段
PERMISSION_REASON_MAX = 500


def _normalize_permission_reason(value: Any) -> str:
    """用户填的拒绝原因：折行压成空格、去掉首尾空白、限长。没填（或不是字符串）给空串。"""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) > PERMISSION_REASON_MAX:
        text = text[: PERMISSION_REASON_MAX - 1] + "…"
    return text


def _permission_denied_reason(name: str, decision: str, reason: str = "") -> str:
    """没批准时给模型看的那句话：说清"没执行"，并区分拒绝 / 回合被停。

    reason 是用户在拒绝时填的原因（可选，卡上那个输入框）：原样带上，模型据此换策略，
    不用去猜"用户为什么不要"。只有拒绝才有原因——"回合被停"时人根本没在答。
    """
    label = _permission_tool_label(name)
    if decision == "stopped":
        return f"回合被停止，本次「{label}」调用没有执行。"
    text = f"用户拒绝权限：本次「{label}」调用没有执行。"
    if reason:
        text += f"用户填写的拒绝原因：{reason}"
    return text
