"""子代理（subagent）：把大批量的复核/探索工作拆给并行的子代理。"""

from __future__ import annotations

import fnmatch
import json
import os
import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TYPE_CHECKING

from GalTransl.Agent.context import (
    _estimate_usage_tokens,
    _find_compaction_cut,
    _keep_recent_tokens,
    _llm_timeout,
    _local_fallback_summary,
    _sanitize_tool_args,
    _tools_overhead_tokens,
)
from GalTransl.Agent.core import (
    AgentStopRequested,
    COMPACT_TRIGGER_RATIO,
    CONTEXT_RESERVE_TOKENS,
    DEFAULT_CONTEXT_WINDOW,
    LLM_MAX_RETRIES,
    REASONING_FIELD_NAMES,
    SUBAGENT_AGENTS,
    SUBAGENT_AGENT_EXPLORE,
    SUBAGENT_AGENT_PROOFREAD,
    SUBAGENT_COMPACT_KEEP_RECENT_RATIO,
    SUBAGENT_EXPLORE_REPORT_CHARS,
    SUBAGENT_LABELS,
    SUBAGENT_MAX_ROUNDS,
    SUBAGENT_MAX_TASKS,
    SUBAGENT_PROGRESS_TICK,
    SUBAGENT_REPORT_CHARS,
    SUBAGENT_TOOL_RESULT_CHARS,
    _classify_llm_error,
    _llm_retry_delay_ms,
    _log,
    _truncate_text,
)
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.prompts import _parse_compact_summary
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Agent.tools.cache import (
    _cache_read_action,
    _tool_list_transl_cache,
    _tool_patch_transl_cache,
    _tool_read_transl_cache,
)
from GalTransl.Agent.tools.common import _split_problem_types
from GalTransl.Agent.tools.input import _list_input_payload
from GalTransl.Agent.tools.listing import _list_grep, _list_limit, _list_order
from GalTransl.Agent.tools.problems import _tool_list_problems
from GalTransl.Agent.tools.render_md import _render_tool_result_table

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- 子代理（subagent）----
#
# 概念与 PI-Desktop 的一致：主 Agent 把"一份可以独立完成的工作"交给子代理——子代理有自己的
# system prompt、自己的消息历史、**受限的工具集**（拿不到委派工具，所以不会递归），跑完只交回
# 一份报告。它中间的思考与工具调用不进主 Agent 的上下文（省 token），但会作为事件推给界面
# （见 subagent_* 事件），所以用户看得到它在干什么。
#
# 子代理也有自己的上下文预算：工具往返堆超窗口时按父 Agent 的同一套规则压缩——窗口取自父
# Agent 的 contextWindow、切点复用 _find_compaction_cut、摘要复用 _summarize_messages。
# 压缩发生在子代理自己的消息里，主 Agent 拿到的仍是那份最终报告。
#
# 与 PI-Desktop 的一处刻意差异：那边一个 Task 调用只起一个子代理、**立即返回** delegationId，
# 再由 TaskWait／自动 resume 收口；我们这里**一次调用带一批任务、阻塞到全部跑完**。原因是
# 我们的回合里工具是串行执行的（见 run() 的 for tc in tool_calls），没有 resume 那套机制，
# 阻塞式最省事也最不容易出错；并发一点没少——同一批里的子代理是真并行跑的。

# 校对子代理的 system prompt：**只能提意见**（写 proofread_comment），不能改译文。
# 写"校对建议"还是"润色建议"由主 Agent 问过用户后写在任务说明里（见 _SUBAGENT_BRIEF_TEMPLATE），
# 这里只把两类意见的定义、写法与优先级讲清楚；任务说明没提时默认只写校对建议。
SUBAGENT_PROOFREAD_PROMPT = """你是 GalTransl 的**校对子代理**，只干一件事：读完分配给你的缓存，把意见写进缓存条目的 proofread_comment。

# 权力边界（越界即失败）
- 你**只能读**（缓存、人名表、翻译规范、问题清单），以及用 patch_transl_cache **写 proofread_comment** 这一个字段；
- 你的 patch_transl_cache 里只有 index + proofread_comment（加一个 file）三个入参：**译文字段（pre_dst / proofread_dst）根本不存在**，也没有委派、启动任务、改配置的权力；
- **你只负责这一次派给你的那些文件**（可能是一个，也可能是自动均分出来的一组）：read_transl_cache / patch_transl_cache 的 filename 只接受它们（patch 支持每条带 file，一样只认这一组），范围外会被直接拒掉；list_problems 也只会列这些文件的问题；要核对某个词在别处的译法，用 read_transl_cache(action="search")（搜索是全项目范围）；
- 发现问题就写意见，改由主 Agent 做——不要试图绕路。

# 写哪一类意见：以任务说明为准
你的意见分两类，**这一遍写哪一类由任务说明（"主 Agent 的额外要求"）指定**——那里面已经写明用户要的是什么：
- 只要求「校对建议」→ 只挑硬伤，不要顺带报风格偏好；
- 只要求「润色建议」→ 只提表达上的改进，别去纠结对错（顺手看到明显硬伤也可以带上一条）；
- 两类都要求 → 都写，但**硬伤优先**：先保证错译漏译都被抓出来，再谈润色。
任务说明没提这件事时，默认**只写校对建议**。

**校对建议（硬伤，四类）**
1. **错译**：意思翻错、主客颠倒、否定/时态/数量弄反；
2. **漏译**：原文有的信息译文里没有（整句漏掉、半句被吞、人称/称谓被省掉）；
3. **事实错误**：人名/地名/专有名词/设定的译法与项目既有译法或原文设定冲突（先查人名表与其它出现处）；
4. **明显不通**：中文不成句、指代错乱、说话人张冠李戴（结合 speaker 判断）。

**润色建议（没硬伤，但中文能更好）**
- 读起来别扭、有翻译腔：词序拗口、修饰语堆叠、一连串"的"；
- 口语不自然：对话像书面语，语气与角色设定（傲娇 / 冷淡 / 大小姐 / 死党…）对不上；
- 用词单调或不准：同一段反复用同一个词，拟声词、感叹处理得生硬；
- 节奏问题：该断句的地方拖成一长串，或该一口气说完的被拆得很碎。
写润色建议必须**给出具体改法**（"建议改成……"）——只说"不够好""可以更自然"等于没写；也不要为了凑数硬提，一条条目最多一条润色意见，优先挑真正影响阅读的。

**两类共同的三条规矩**：一条条目只写一次（再写会覆盖）；没问题的条目不要写；每条都要写清"问题是什么 + 该怎么改"，必要时给出原文依据。宁可少报，也不要拿噪音把真正的硬伤淹掉。

# 怎么干
1. 先 read_transl_cache 读你负责的区间（默认列就够：原文 post_src、译文 pre_dst、机翻自查 problem，以及别人写过的 proofread_comment）；
2. 要判断译名一致性：read_transl_cache(action="search") 搜同一个词的其它出现处、get_name_table 看人名表；判断取舍时 read_guideline 看项目规范（润色建议尤其要以项目规范为准，别跟规范里定下的文风打架）；
3. 有疑问就用 patch_transl_cache 写进去，一条一个问题、写清"问题是什么 + 该怎么改"。可以一次多条：
   patch_transl_cache(filename="<你的文件>", patches=[
     {"index": 33, "proofread_comment": "漏译：原文「おっぱい」在译文里没有对应词，建议补为「欧派」"},
     {"index": 41, "proofread_comment": "错译：原文是「否定」，译文翻成了肯定"},
     {"index": 58, "proofread_comment": "润色：直译得比较生硬，建议改成「我才不是特意为你做的呢！」"},
   ])
   没问题的条目不要写；同一个 index 只写一次（再写会覆盖）。
4. problem 里的机翻提示可以参考，但那是统计标签，**只写你核对过的**，不要照抄。
5. 读不完就分段读（index 支持区间），不要为了省事跳过没读的条目——你没读的部分等于没校对。

# 收尾
不再调用工具后，输出一份**简短**报告（这是主 Agent 唯一会看到的你的输出）：
- 负责的文件与区间、读了多少条；
- 写了几条意见、分别是哪一类（校对：错译/漏译/事实错误/不通；润色：表达/语气/用词/节奏）；
- 拿不准但值得人看一眼的点。
不要在报告里复述每条意见的全文（proofread_comment 里已经有了），也不要贴原文译文。"""

# 子代理的 user 消息（任务说明）。校对：文件是任务的天然边界——不同子代理写不同文件的
# proofread_comment，互不打架；同一个人也能只领一个区间（或自动均分出来的一组文件）。
_SUBAGENT_BRIEF_TEMPLATE = """# 你的任务

- 角色：{label}
- 负责的缓存文件：{file}
- 负责的区间：{indexes}
- 主 Agent 的额外要求（**里面会写明这一遍写哪一类意见：校对建议 / 润色建议 / 两者都要**）：{brief}

负责的文件可能不止一个（自动均分出来的），逐个文件处理，别漏；读完你负责的区间，把发现的
问题写进对应条目的 proofread_comment，然后交报告。上面没写意见类型时，默认只写校对建议。"""

# 原文探索的任务说明：它不写文件，交的只有报告。
_SUBAGENT_EXPLORE_BRIEF = """# 你的任务

- 角色：{label}
- 负责的原文：{file}
- 主 Agent 的额外要求：{brief}

按上面的流程探索，最后交报告（不写任何文件，不要改字典、也不要改规范）。"""


# 原文探索子代理的 system prompt：**它读的是原文，不是译文**——目标是"字典还缺什么"与
# "翻译规范该注意什么"，产出只有一份报告（落地由主 Agent 做）。定位是 GenDic 的补充：
# 自动生成会漏掉的昵称、低频专有名词、口头称呼，靠通读原文找。
SUBAGENT_EXPLORE_PROMPT = """你是 GalTransl 的**原文探索子代理**，只干两件事：读**原文**、对照 **GPT 字典**，找出「字典里还缺什么」与「翻译规范该注意什么」，把结论写进最后那份报告。

# 权力边界（越界即失败）
- 你**只能读**两样东西：输入目录里的原文（list_input_files / read_input_file / search_input）与项目 GPT 字典（list_dict_files / read_dict）；
- 你**不写任何文件**，也看不到译文：字典与项目规范由主 Agent 汇总后落地。你的价值在"读得广、找得准"，不在动手改；
- 任务里点名了原文文件的话，你就**只负责点名给你的那些**（可能不止一个，自动均分出来的，逐个处理别漏）：list_input_files 只会列出它们，读别的文件会被拒；没点名才由你自己挑。

# 找什么
1. **GPT 字典的缺口（主要目标）**，尤其是自动生成（GenDic）会漏的那类：
   - 人名、**昵称 / 爱称 / 绰号**，以及"同一个人被叫好几个名字"的情况（给出判断为同一人的依据）；
   - 专有名词：地名、组织、道具、招式、设定名词；
   - 特殊称呼与亲属称谓（お兄ちゃん、先輩、〜様 这类）；
   - 口癖、自造词、以及假名同音容易翻错的词。
2. **翻译规范建议（次要目标）**：称谓与人称取舍（敬称是否保留、さん / ちゃん 怎么落）、文体与语气（书面或口语、口癖要不要译出）、标点与符号（「」是否保留、省略号、感叹号）、以及"哪些词必须统一译法"。

# 怎么干
1. 先 list_input_files 看有哪些原文文件，挑代表性的读——**别试图读完整个项目**，预算用完就停，按文件顺序来；
2. 用 read_input_file 读原文（index 从 1 开始，支持区间如 "1-100"）；判断只能基于原文本身与字典，你没有译文可参考；
3. 用 **search_input** 核对"某个词/称呼全篇出现过多少次、都在什么上下文、是不是同一个角色在用"（context=2~3 一起看上文）——收不收进字典、收哪个写法，靠的是这些次数与场景，别凭一次偶遇下结论；
4. 用 list_dict_files / read_dict 看 GPT 字典里**已经有什么**：只报没有的或写得不好的，重复的建议是噪音；
5. 提到一个词时把信息给准：原文写法、出现处（文件 / index）、大约出现多少次（search_input 的 total 就是）、为什么该收进字典。

# 收尾
不再调用工具后，输出一份**紧凑的结构化报告**（这是主 Agent 唯一会看到的你的输出，有长度上限）：
## 字典候选
原文 → 建议译法 ｜ 出现处 ｜ 一句话依据（一行一条，按重要性排序：影响理解的、出现多的排前面）
## 规范建议
一句话一条，写清"什么情况 → 怎么处理"，不要写"注意语气"这类空话
## 拿不准的
需要人来定的取舍（列出选项与各自的代价）

不要复述原文成段内容，不要写剧情概述，不要提议与字典和规范无关的东西。"""


# 子代理唯一能写的字段：校对批注。它用的就是主 Agent 那个 patch_transl_cache，只是入参 schema
# 被摘得只剩 proofread_comment、handler 那头也只放得住这一个字段（双保险）。
SUBAGENT_PATCHABLE_FIELDS: frozenset[str] = frozenset({"proofread_comment"})


@dataclass(frozen=True)
class SubAgentRole:
    """一个子代理角色的规格：提示词、任务说明模板、工具白名单、要不要锁缓存文件、报告上限。

    加角色 = 在 SUBAGENT_ROLES 里加一条并写好 prompt。**工具白名单是"能不能干这件事"的唯一
    依据**（不是提示词），所以每个角色的 tools 都要显式列全；白名单里出现的名字必须都在
    _TOOL_HANDLERS 里（有测试盯着）。
    """

    prompt: str
    brief: str
    tools: tuple[str, ...]
    # 是否必须锁定一个缓存文件（校对要，靠文件边界避免两个子代理写同一条；原文探索不需要）
    needs_file: bool = False
    report_chars: int = SUBAGENT_REPORT_CHARS


SUBAGENT_ROLES: dict[str, SubAgentRole] = {
    SUBAGENT_AGENT_PROOFREAD: SubAgentRole(
        prompt=SUBAGENT_PROOFREAD_PROMPT,
        brief=_SUBAGENT_BRIEF_TEMPLATE,
        tools=(
            "read_transl_cache",
            "list_problems",
            "get_name_table",
            "read_guideline",
            "patch_transl_cache",
        ),
        needs_file=True,
    ),
    SUBAGENT_AGENT_EXPLORE: SubAgentRole(
        prompt=SUBAGENT_EXPLORE_PROMPT,
        brief=_SUBAGENT_EXPLORE_BRIEF,
        # 只有原文与 GPT 字典：不碰缓存（它看的是原文）、不碰规范（建议由主 Agent 合并时取舍），
        # 更没有任何写工具。search_input 给它"某个称呼全篇出现过几次、都在什么上下文"这类
        # 判断用——它要的正是"读得广"。
        tools=("list_input_files", "read_input_file", "search_input", "list_dict_files", "read_dict"),
        report_chars=SUBAGENT_EXPLORE_REPORT_CHARS,
    ),
}


def _subagent_role(agent: str) -> SubAgentRole:
    """取角色的规格；不认识的角色直接报错（入口处也校验一次，这里是兜底）。"""
    role = SUBAGENT_ROLES.get(agent)
    if role is None:
        raise AgentToolError(
            f"不认识的子代理角色：{agent!r}（可用：{'、'.join(SUBAGENT_ROLES)}）"
        )
    return role


def _subagent_patch_schema() -> dict[str, Any]:
    """子代理版的 patch_transl_cache：**把 pre_dst / proofread_dst 两个入参摘掉**，只留 proofread_comment。

    顶层 clear_comment 同样摘掉：那是"复核完的人"成批清批注用的，子代理只写意见（它不看这些
    意见处理没处理），留着只会让它把别人刚写下、还没处理的批注误清掉。

    与 handler 侧的窄白名单（SUBAGENT_PATCHABLE_FIELDS）一起构成"改不了译文"的双保险——
    这件事由代码保证，不靠提示词自觉。schema 从主 Agent 那份深拷贝再改，避免哪天主 Agent
    换了描述、子代理这份漂移。
    """
    for tool in AGENT_TOOLS:
        function = tool.get("function") or {}
        if function.get("name") != "patch_transl_cache":
            continue
        copy: dict[str, Any] = json.loads(json.dumps(tool, ensure_ascii=False))
        properties = copy["function"]["parameters"]["properties"]["patches"]["items"]["properties"]
        for field in ("pre_dst", "proofread_dst"):
            properties.pop(field, None)
        copy["function"]["parameters"]["properties"].pop("clear_comment", None)
        copy["function"]["description"] = (
            "把你的意见写进缓存条目的 proofread_comment（校对批注），一条一个具体问题，"
            "写清问题在哪、该怎么改。可以一次传多条 patches。"
            "**写校对建议还是润色建议以任务说明为准**（没说明就默认只写校对建议）。"
            "**你是校对子代理，只能写 proofread_comment**——译文字段不在你的入参里，改由主 Agent 做；"
            "同一个 index 写第二次会覆盖上一次。"
        )
        return copy
    raise RuntimeError("AGENT_TOOLS 里找不到 patch_transl_cache")


def _subagent_tools(agent: str) -> list[dict[str, Any]]:
    """某个角色的工具表：从主 Agent 的工具表里按它的白名单挑，patch_transl_cache 换成收窄版。"""
    role = _subagent_role(agent)
    picked: list[dict[str, Any]] = []
    for tool in AGENT_TOOLS:
        name = str((tool.get("function") or {}).get("name") or "")
        if name not in role.tools:
            continue
        picked.append(_subagent_patch_schema() if name == "patch_transl_cache" else tool)
    return picked


# 认"锁定文件"的工具：任务里给了 file 时，这些工具的 filename 入参被限制在派给它的那些文件里。
# 搜索（read_transl_cache 的 action=search / search_input）与 get_name_table 本来就是跨文件/
# 跨项目的，不锁——子代理要核对"这个词在别处怎么翻的/原文里怎么说"，正是它们的用途。
# read_transl_cache 只锁 action=read（list 只列锁定的文件），见 _lock_cache_reader。list_problems 没有
# filename 入参，改由 _subagent_handlers 传 allowed_files 收窄（见 _tool_list_problems）。
_LOCKED_FILENAME_TOOLS: tuple[str, ...] = (
    "patch_transl_cache",
    "read_input_file",
)

# 任务里 file 填这个值 = 自动均分：本批里**同一角色**的每个 "*" 任务平分该角色的全部文件
# （校对=缓存文件，原文探索=原文文件）。例如派 16 个 "*"、项目有 256 个缓存文件 → 每个 16 个。
SUBAGENT_FILE_ALL = "*"

# file 的选择器前缀：把"选谁"从"单个文件名 / *"两档扩成一层（选择器 → 有序文件清单）：
#   list:   清单（list:a.json,b.json）
#   glob:   通配（glob:SW_01_*）
#   regex:  正则（regex:^0[12]_）
#   select: 吃 list_problems 的结果集：select:has_problem / select:problem_type=<类型>
#   random: 从候选里随机挑 N 个（random:5）——前期探索用
# 认不出的写法按"单个文件名"处理（向后兼容）。选择器解析出的是"选谁"，与 count/indexes 正交。
SUBAGENT_FILE_LIST = "list:"
SUBAGENT_FILE_GLOB = "glob:"
SUBAGENT_FILE_REGEX = "regex:"
SUBAGENT_FILE_SELECT = "select:"
SUBAGENT_FILE_RANDOM = "random:"


def _subagent_file_counts(runner: AgentRunner, agent: str) -> dict[str, int]:
    """某个角色"可分派的文件" → 条数（按名字排序：顺序稳定，均分/切片结果可复现）。

    - 校对：缓存目录里的缓存文件（只认 `.json`）——跳过条目数为 0 的（没什么可校对，
      派过去等于白烧一个 agent 的 token）；
    - 原文探索：输入目录里的原文文件（条数取解析出的 sentences，未知算 0）。
    条数只在"文件比 count 少、要把大文件按 index 切开"时用得到。
    """
    pid = runner._project_id()
    counts: dict[str, int] = {}
    if agent == SUBAGENT_AGENT_PROOFREAD:
        data = runner._http_get(f"/api/projects/{pid}/cache")
        for item in data.get("files", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name or not name.endswith(".json"):
                continue
            count = item.get("entry_count")
            if isinstance(count, int) and count <= 0:
                continue
            counts[name] = count if isinstance(count, int) else 0
    else:
        cfg = urllib.parse.quote(runner.state.config_file_name or "config.yaml")
        data = runner._http_get(f"/api/projects/{pid}/files?counts=1&config={cfg}")
        for item in data.get("input_files", []):
            if not isinstance(item, dict) or not item.get("is_file", True):
                continue
            name = str(item.get("name") or "")
            if name:
                counts[name] = int(item.get("sentences") or 0)
    return dict(sorted(counts.items()))


def _subagent_problem_files(runner: AgentRunner, problem_type: str = "") -> set[str]:
    """有问题（或含指定问题类型）的文件集合——直接吃 list_problems 的结果集。

    problem_type 为空 = 只要有问题的文件；给了则按类型过滤（口径同 list_problems：
    英文逗号分隔、取「类型：详情」的类型前缀做子串匹配）。
    """
    pid = runner._project_id()
    cfg = urllib.parse.quote(runner.state.config_file_name or "config.yaml")
    data = runner._http_get(f"/api/projects/{pid}/problems?config={cfg}")
    wanted = [t.strip() for t in problem_type.split(",") if t.strip()]
    files: set[str] = set()
    for item in data.get("problems", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "")
        if not name:
            continue
        if wanted and not any(w in _split_problem_types(item.get("problem", "")) for w in wanted):
            continue
        files.add(name)
    return files


def _resolve_subagent_files(
    runner: AgentRunner, agent: str, spec: str, pool: list[str]
) -> list[str]:
    """把 file 的选择器解析成有序文件清单；具体文件名原样返回（向后兼容）。

    - pool 是该角色的全部候选文件，"*"/glob/regex/select/random 都在它里面挑；
    - "list:" 直接照单全收（点名优先，允许写出候选之外的名字，锁定那层会兜底）。
    """
    text = spec.strip()
    if text == SUBAGENT_FILE_ALL:
        return list(pool)
    if text.startswith(SUBAGENT_FILE_LIST):
        names: list[str] = []
        for name in (part.strip() for part in text[len(SUBAGENT_FILE_LIST):].split(",")):
            if name and name not in names:
                names.append(name)
        return names
    if text.startswith(SUBAGENT_FILE_GLOB):
        pattern = text[len(SUBAGENT_FILE_GLOB):].strip()
        return [name for name in pool if fnmatch.fnmatch(name, pattern)]
    if text.startswith(SUBAGENT_FILE_REGEX):
        pattern = text[len(SUBAGENT_FILE_REGEX):]
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise AgentToolError(f"file 的 regex 选择器写错了：{exc}") from exc
        return [name for name in pool if compiled.search(name)]
    if text.startswith(SUBAGENT_FILE_SELECT):
        arg = text[len(SUBAGENT_FILE_SELECT):].strip()
        if arg in ("", "has_problem", "*"):
            problem_type = ""
        elif arg.startswith("problem_type="):
            problem_type = arg.split("=", 1)[1].strip()
        else:
            raise AgentToolError(
                "file 的 select 选择器只支持 select:has_problem 与 "
                f"select:problem_type=<类型>（收到 {text!r}）"
            )
        hit = _subagent_problem_files(runner, problem_type)
        return [name for name in pool if name in hit]
    if text.startswith(SUBAGENT_FILE_RANDOM):
        arg = text[len(SUBAGENT_FILE_RANDOM):].strip()
        if not arg.isdigit() or int(arg) < 1:
            raise AgentToolError(f'file 的 random 选择器要写个数，如 "random:5"（收到 {text!r}）')
        return random.sample(pool, min(int(arg), len(pool)))
    return [text]


def _split_files_evenly(files: list[str], parts: int) -> list[list[str]]:
    """把 files 按顺序均分成 parts 份（256 个文件 16 份 → 每份 16 个）。

    除不尽时**前面几份多一个**（10 个文件 3 份 → 4/3/3）：谁也不比谁多挨近一倍。
    文件比份数少时，后面几份是空的——调用方据此把这些任务丢掉（空跑一轮照样烧 token）。
    """
    if parts <= 0:
        return []
    base, extra = divmod(len(files), parts)
    groups: list[list[str]] = []
    start = 0
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        groups.append(files[start : start + size])
        start += size
    return groups


def _split_sizes(total: int, parts: int) -> list[int]:
    """把 total 条均分成 parts 份，除不尽时前面几份多一条（口径同 _split_files_evenly）。"""
    if parts <= 0:
        return []
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def _parse_index_ranges(spec: str) -> list[tuple[int, int]]:
    """把 "1-200,300-400" / "5" 解析成 [(1,200),(300,400)]（口径同 _parse_index_spec）。"""
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        token = part.strip().replace("*", "")
        if not token:
            continue
        if "-" in token:
            bounds = token.split("-", 1)
            try:
                lo, hi = int(bounds[0]), int(bounds[1])
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            ranges.append((lo, hi))
        else:
            try:
                value = int(token)
            except ValueError:
                continue
            ranges.append((value, value))
    return ranges


def _slice_ranges(ranges: list[tuple[int, int]], parts: int) -> list[str]:
    """把若干区间按条数整体切成 parts 段，返回区间字符串（如 ["1-103","104-206"]）。

    用于"一个大文件切给 N 个代理并行"：每个子代理只处理自己那一段。
    """
    total = sum(hi - lo + 1 for lo, hi in ranges)
    if parts <= 0 or total <= 0:
        return []
    sizes = _split_sizes(total, parts)
    out: list[str] = []
    ri = 0
    cursor = ranges[0][0]
    for size in sizes:
        if size <= 0:
            continue
        pieces: list[str] = []
        need = size
        while need > 0 and ri < len(ranges):
            lo, hi = ranges[ri]
            take = min(need, hi - cursor + 1)
            start, end = cursor, cursor + take - 1
            pieces.append(str(start) if take == 1 else f"{start}-{end}")
            cursor += take
            need -= take
            if cursor > hi:
                ri += 1
                if ri < len(ranges):
                    cursor = ranges[ri][0]
        if pieces:
            out.append(",".join(pieces))
    return out


def _expand_subagent_file_selection(
    files: list[str], indexes: str, count: int, counts: dict[str, int]
) -> list[tuple[list[str], str]]:
    """把（选谁 × 切几份 × 取哪段）展开成一组子代理任务：[(文件清单, indexes), ...]。

    三个维度互相独立：
    - count<=1：一个任务，indexes 原样带上（indexes 只对单个文件有意义）；
    - count>1 且给了 indexes：必须只选中一个文件，在区间内再切成 count 段；
    - count>1 且没给 indexes：文件够分就按文件均分；文件不够就把大文件按条数切成
      index 段，凑够 count 个任务（如一个大文件切 4 段并行）。
    """
    if count <= 1:
        if indexes and len(files) > 1:
            raise AgentToolError(
                "indexes 只能用于单个文件（选中的文件有多个，分不清区间属于哪一份）："
                "给 indexes 时请让 file 只选中一个文件。"
            )
        return [(list(files), indexes)]

    if indexes:
        if len(files) != 1:
            raise AgentToolError(
                f"file 选中的文件有 {len(files)} 个，同时给 indexes 与 count 时分不清区间属于哪个文件："
                "请让 file 只选中一个文件，或去掉 indexes 让它按文件均分。"
            )
        ranges = _parse_index_ranges(indexes)
        total = sum(hi - lo + 1 for lo, hi in ranges)
        if total <= 0:
            raise AgentToolError(f"indexes 解析不出有效区间：{indexes!r}")
        if count > total:
            raise AgentToolError(f"indexes 只覆盖 {total} 条，切不成 {count} 段：调小 count。")
        return [([files[0]], seg) for seg in _slice_ranges(ranges, count)]

    if len(files) >= count:
        return [(group, "") for group in _split_files_evenly(files, count) if group]

    # 文件不够分：把每个文件按条数切成 index 段，凑够 count 个任务
    per_file, extra = divmod(count, len(files))
    out: list[tuple[list[str], str]] = []
    for i, name in enumerate(files):
        parts = per_file + (1 if i < extra else 0)
        if parts <= 1:
            out.append(([name], ""))
            continue
        total = int(counts.get(name) or 0)
        if total <= 0:
            raise AgentToolError(
                f"「{name}」的条数未知，没法按 count 切成 {parts} 段：改用多个文件，或减少 count。"
            )
        if parts > total:
            raise AgentToolError(f"「{name}」只有 {total} 条，切不成 {parts} 段：调小 count。")
        out.extend(([name], seg) for seg in _slice_ranges([(1, total)], parts))
    return out


def _selection_split_note(
    agent: str, files: list[str], expansions: list[tuple[list[str], str]], parts: int
) -> str:
    """非 "*" 选择器并行展开后给主 Agent 的一句交代（分了多少、按什么分的）。"""
    label = SUBAGENT_LABELS.get(agent, agent)
    if len(files) >= parts:
        sizes = [len(group) for group, _ in expansions]
        non_empty = [size for size in sizes if size]
        span = str(non_empty[0]) if len(set(non_empty)) == 1 else f"{min(non_empty)}-{max(non_empty)}"
        return f"已把选中的 {len(files)} 个文件均分给 {parts} 个「{label}」子代理（每个 {span} 个）。"
    return f"已把选中的 {len(files)} 个文件按 index 切成 {parts} 段并行。"


def _lock_to_filenames(
    fn: Callable[[AgentRunner, dict[str, Any]], Any], allowed: tuple[str, ...]
) -> Callable[[AgentRunner, dict[str, Any]], Any]:
    """把工具引用的文件名限制在 allowed 里：范围外一律拒绝，并说清这是本次派活的锁定范围。

    引用的文件不只有顶层 filename：patch_transl_cache 支持每条 patch 自带 file（跨文件批量，
    见 _group_cache_patches_by_file），那也得逐条查——否则子代理靠 patches[].file 就绕过了锁定。
    """

    def scope_text() -> str:
        if len(allowed) == 1:
            return f"你只负责「{allowed[0]}」这一个文件（本次派活的锁定范围）"
        shown = "、".join(f"「{name}」" for name in allowed[:6])
        more = f" 等 {len(allowed)} 个文件" if len(allowed) > 6 else ""
        return f"你只负责这 {len(allowed)} 个文件（本次派活的锁定范围）：{shown}{more}"

    def referenced(args: dict[str, Any]) -> list[str]:
        names: list[str] = []
        top = str(args.get("filename", "") or "").strip()
        if top:
            names.append(top)
        for patch in args.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            name = str(patch.get("file", "") or "").strip()
            if name:
                names.append(name)
        return list(dict.fromkeys(names))

    def wrapped(runner: AgentRunner, args: dict[str, Any]) -> Any:
        asked = referenced(args)
        if not asked:
            raise AgentToolError(f"{scope_text()}：这次没给 filename。")
        outside = [name for name in asked if name not in allowed]
        if not outside:
            return fn(runner, args)
        raise AgentToolError(
            f"{scope_text()}：「{'」「'.join(outside)}」不在范围里。要看别的文件，让主 Agent 重新派任务。"
        )

    return wrapped


def _lock_cache_reader(allowed: tuple[str, ...]) -> Callable[[AgentRunner, dict[str, Any]], Any]:
    """read_transl_cache 在锁定模式下：read 只认派给它的文件；list 只列这些文件；
    search 照旧全项目（核对别处译法正是它的用途）。"""
    read_locked = _lock_to_filenames(
        lambda runner, args: _tool_read_transl_cache(runner, {**args, "action": "read"}), allowed
    )

    def wrapped(runner: AgentRunner, args: dict[str, Any]) -> Any:
        action = _cache_read_action(args)
        if action == "read":
            return read_locked(runner, args)
        if action == "list":
            out = _tool_list_transl_cache(runner, args, names=allowed)
            scope = "、".join(f"「{name}」" for name in allowed[:6]) + (
                f" 等 {len(allowed)} 个文件" if len(allowed) > 6 else ""
            )
            note = f"本次只派你看 {scope}，其余文件不在你的范围里（要处理别的文件，让主 Agent 重新派任务）。"
            merged = "；".join([out["note"], note]) if out.get("note") else note
            return {"action": "list", **out, "note": merged}
        return _tool_read_transl_cache(runner, args)

    return wrapped


def _lock_input_listing(
    allowed: tuple[str, ...],
) -> Callable[[AgentRunner, dict[str, Any]], Any]:
    """list_input_files 在锁定模式下只列自己负责的那几份原文；一个都对不上就照实说。"""

    def wrapped(runner: AgentRunner, args: dict[str, Any]) -> Any:
        # 过滤交给 _list_input_payload 的 names（**先过滤再采样**）：自己过滤采样后的结果，
        # 会把"本来属于它、但被采样摇掉"的文件误判成"不在原文清单里"。
        out = _list_input_payload(
            runner,
            grep=_list_grep(args),
            limit=_list_limit(args),
            order=_list_order(args),
            names=allowed,
        )
        if not out.get("input_files"):
            # 锁定的名字一个都不在原文清单里（多半是文件名写错了）：照旧全列，但把话说清楚，
            # 免得它对着空清单发懵
            full = _list_input_payload(
                runner, grep=_list_grep(args), limit=_list_limit(args), order=_list_order(args)
            )
            return {
                **full,
                "note": (
                    "注意：本次任务锁定的文件都不在原文清单里（检查一下文件名），"
                    "下面是全部原文文件。"
                ),
            }
        if len(allowed) == 1:
            note = (
                f"本次只派你看「{allowed[0]}」这一个文件，其余文件不在你的范围里"
                "（要处理别的文件，让主 Agent 重新派任务）。"
            )
        else:
            # 报的是**职责范围**（count）而不是这一屏显示了几行（returned）：采样只管显示
            note = (
                f"本次只派你看这 {out['count']} 个文件，其余文件不在你的范围里"
                "（要处理别的文件，让主 Agent 重新派任务）。"
            )
        return {**out, "note": "；".join([out["note"], note]) if out.get("note") else note}

    return wrapped


def _subagent_handlers(
    agent: str, locked_files: str | Sequence[str] = ()
) -> dict[str, Callable[[AgentRunner, dict[str, Any]], Any]]:
    """某个角色可用的 handler（按它的白名单从主 Agent 那张表里取）。

    - patch_transl_cache 包一层窄白名单：只放得住 proofread_comment（译文字段即使模型硬塞也会被
      当成"无可更新字段"跳过，并被回一条只允许 proofread_comment 的工具错误）；
    - **锁定文件**（locked_files 非空，来自任务里的 file；自动均分时是一组）：read_transl_cache 的
      read、patch_transl_cache / read_input_file 的 filename 被限制在这一组里，read_transl_cache 的
      list 与 list_input_files 也只列这些，list_problems 也只列这些文件的问题——"一份文件只归一个子代理"由工具层保证，模型串到
      范围外会被拒（省 token，也避免两个子代理写同一条）。要核对"这个词在别处怎么翻的"，仍走
      跨文件的 read_transl_cache(action="search") / get_name_table；
    - 调用**不过权限门禁**：子代理的工具集本身就是白名单（只有读，加校对那一支写意见），
      一批 16 个逐条弹审批卡会把界面淹掉。改译文的权力仍然只在主 Agent 手上——那才是要审批的事。
    """
    # 延迟导入：注册表要登记 run_subagents（本模块），模块顶层互相导入会成环
    from GalTransl.Agent.handlers import _TOOL_HANDLERS

    role = _subagent_role(agent)
    handlers: dict[str, Callable[[AgentRunner, dict[str, Any]], Any]] = {
        name: _TOOL_HANDLERS[name] for name in role.tools if name in _TOOL_HANDLERS
    }
    if "patch_transl_cache" in role.tools:
        handlers["patch_transl_cache"] = lambda runner, args: _tool_patch_transl_cache(
            runner, args, SUBAGENT_PATCHABLE_FIELDS
        )
    # 裸字符串按"一个文件名"处理（Sequence[str] 会把字符串按字符拆开——那是陷阱不是功能）
    locked_names: Sequence[str] = (locked_files,) if isinstance(locked_files, str) else locked_files
    locked = tuple(str(name).strip() for name in locked_names if str(name).strip())
    if locked:
        for name in _LOCKED_FILENAME_TOOLS:
            if name in handlers:
                handlers[name] = _lock_to_filenames(handlers[name], locked)
        if "read_transl_cache" in handlers:
            handlers["read_transl_cache"] = _lock_cache_reader(locked)
        if "list_input_files" in handlers:
            handlers["list_input_files"] = _lock_input_listing(locked)
        if "list_problems" in handlers:
            handlers["list_problems"] = lambda runner, args: _tool_list_problems(runner, args, locked)
    return handlers


def _subagent_chat(
    client: Any, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> tuple[str, list[Any], str, str]:
    """子代理的一次请求（**非流式**）：返回（正文, 工具调用, 思考字段名, 思考内容）。

    非流式是刻意的简化：子代理的中间输出不需要逐字上屏，一轮一次拿全更简单。代价是"停止"
    要等当前这次请求回来才生效（父回合的停止仍会立刻终止它后续的轮次）。
    思考字段的约定与主 Agent 一致（见 REASONING_FIELD_NAMES）：带 tools 的多轮对话里，
    DeepSeek 这类 provider 要求把上一轮的 reasoning 原样回传，不回就 400。

    tools=None 用于压缩那一轮：整个字段不发出（而不是发 null——有些兼容端点不认），
    模型因此没有"接着调工具"的选项（见 SubAgentRunner._begin_compaction）。
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "timeout": _llm_timeout(),
    }
    if tools is not None:
        kwargs["tools"] = tools
    resp = client.chat.completions.create(**kwargs)
    choices = getattr(resp, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    if message is None:
        return "", [], REASONING_FIELD_NAMES[0], ""
    content = str(getattr(message, "content", "") or "")
    tool_calls = list(getattr(message, "tool_calls", None) or [])
    field, reasoning = "", ""
    for name in REASONING_FIELD_NAMES:
        value = getattr(message, name, None)
        if value:
            field, reasoning = name, str(value)
            break
    return content, tool_calls, field, reasoning


# 子代理的压缩指令（Insert-then-Compress 的那条瞬时消息，见 SubAgentRunner._begin_compaction）。
# 与主 Agent 那份（COMPACT_INSTRUCTION_PROMPT）的差别：子代理任务单一，而且压缩那一轮**不带
# tools**——它没有"接着调工具"的余地，所以不必像主 Agent 那样反复强调"不要执行上面的请求"。
# 输出同样用 <summary> 包住，与主 Agent 共用 _parse_compact_summary 解析。
SUBAGENT_COMPACT_INSTRUCTION_PROMPT = """[记忆压缩模式] 上面的工作已经告一段落。现在不要继续任务，把它压缩成一份摘要，供你在后续（换了一段上下文之后）接着做同一件事。严格执行：
1. 只输出摘要，不要调用工具、不要接着干活；
2. 正文用 <summary>…</summary> 包住，按下面的骨架写：
<summary>
## 任务与范围
（你负责的文件 / index 区间、目标）
## 已完成
（读过哪些区间、做了什么、写下了哪些意见或发现）
## 关键发现
（逐条列：文件名 / index / 原文写法 / 译名 / 结论——这些硬信息必须原样保留，不要概括掉）
## 待办
（还没读的区间、需要复查或拿不准的点）
</summary>
3. 用中文，简洁但不丢信息。"""


class SubAgentRunner:
    """一个子代理实例：自己的消息、自己的工具表，跑完交一份报告。

    一个实例只被一个线程跑（见 _tool_run_subagents 的线程池），所以内部不需要加锁。
    消息历史超窗口时与父 Agent 走同一套压缩（Insert-then-Compress：挂上压缩指令、用下一轮
    请求把摘要拿回来，见 _begin_compaction），避免 24 轮工具往返把上下文撑爆。
    """

    def __init__(
        self,
        parent: AgentRunner,
        *,
        agent: str,
        files: Sequence[str],
        indexes: str,
        brief: str,
        delegation_id: str,
    ) -> None:
        self.parent = parent
        self.agent = agent
        self.role = _subagent_role(agent)  # 提示词 / 工具白名单 / 报告上限都从它取
        # 派给它的文件（一个或一组；自动均分时是一组）。工具层按它限范围，见 _subagent_handlers
        self.files: tuple[str, ...] = tuple(str(name).strip() for name in files if str(name).strip())
        self.indexes = indexes
        self.brief = brief
        self.id = delegation_id
        self.messages: list[dict[str, Any]] = []
        self.turns = 0
        self.tool_calls = 0
        # 它写进缓存的那几条校对批注（{file, index, content}）：字段名跟缓存里的
        # proofread_comment 对齐，报告里只回 file + index，全文在缓存里
        self.proofread_comments: list[dict[str, Any]] = []
        self.started_at = time.time()
        # 正在进行的一次压缩：{cut, head_keep, estimated, limit}。挂上压缩指令后置上，
        # 收尾（_finish_compaction）或回滚（_abort_compaction）时清空。
        self._pending_compaction: dict[str, Any] | None = None
        # 压缩整条路子都失败过（插入式 + 独立请求都没压成）：不再重试，否则每轮都白跑一次
        self._compact_failed = False

    @property
    def file_label(self) -> str:
        """行上/结果里显示用的一行：单个就是文件名，一组是「首个 等 N 个文件」（完整清单在 files）。"""
        if not self.files:
            return ""
        if len(self.files) == 1:
            return self.files[0]
        return f"{self.files[0]} 等 {len(self.files)} 个文件"

    @property
    def file_list_text(self) -> str:
        """任务说明里给子代理看的**完整**清单——它得知道自己负责哪些（没有列表类工具可查）。"""
        return "、".join(self.files) if self.files else "全部（自己按清单挑）"

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """子代理事件：一律带自己的 id，界面据此挂到发起它的那行下面。"""
        self.parent._emit(event_type, {"id": self.id, **data})

    def _finish(self, status: str, report: str, error: str = "") -> dict[str, Any]:
        text = report.strip()
        limit = self.role.report_chars
        if len(text) > limit:
            text = text[:limit] + "…（报告已截断）"
        result: dict[str, Any] = {
            "id": self.id,
            "agent": self.agent,
            "label": SUBAGENT_LABELS.get(self.agent, self.agent),
            # file 是给人看的一行（一组时是「首个 等 N 个」），files 是完整清单——
            # 主 Agent 后面要按文件去读/清 proofread_comment，必须有准名字
            "file": self.file_label,
            "files": list(self.files),
            "indexes": self.indexes,
            "status": status,
            "report": text,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            # 只回 文件 + index：意见全文在缓存里，主 Agent 需要细节就读那几条。
            # 带上 file 是因为一个子代理可能负责一组文件，只给 index 认不出是哪一份里的。
            # 字段名与缓存里的批注字段一致（proofread_comment）。
            "proofread_comment": [
                {"file": d.get("file", ""), "index": d.get("index")}
                for d in self.proofread_comments
            ],
            "duration_ms": int((time.time() - self.started_at) * 1000),
        }
        if error:
            result["error"] = error
        self._emit(
            "subagent_done",
            {
                "status": status,
                "report": text,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "proofread_comment": len(self.proofread_comments),
                "duration_ms": result["duration_ms"],
                # 结束时间戳（Unix 秒）：与 started_at 配对，界面重放后也能还原区间
                "finished_at": time.time(),
                "error": error,
            },
        )
        return result

    # ---- 上下文窗口复用与压缩 ----

    def _parent_context_window(self) -> int:
        """复用父 Agent 解析出的上下文窗口：同一个后端配置，子代理没理由再解析一遍。"""
        try:
            window = int(getattr(self.parent, "_context_window", 0) or 0)
        except (TypeError, ValueError):
            window = 0
        return window or DEFAULT_CONTEXT_WINDOW

    def _request_overhead_tokens(self) -> int:
        """这次请求里 messages 之外的固定开销：它自己那套（收窄过的）tools schema。"""
        return _tools_overhead_tokens(_subagent_tools(self.agent))

    def _estimate_context_tokens(self) -> int:
        """估算当前请求占用的 token（子代理无 usage 锚点，纯字符估算；见 _estimate_usage_tokens）。"""
        return _estimate_usage_tokens(self.messages, overhead=self._request_overhead_tokens())

    def _begin_compaction(self) -> bool:
        """历史超窗口就挂上压缩指令，让**下一轮请求**顺带把摘要拿回来（Insert-then-Compress）。

        与父 Agent 同一机制、同一套阈值（父的窗口 + COMPACT_TRIGGER_RATIO + 尾部预留），
        差别只有三处：
        - 头部 2 条（system 提示词 + 任务说明）永远保留——子代理没有别的途径知道"我是谁、
          负责哪些文件"；
        - 保留段按 token 预算挑，预算取父会话的一半（SUBAGENT_COMPACT_KEEP_RECENT_RATIO）；
        - 压缩那一轮**不带 tools**，它没有"接着调工具"的余地，也就没有父 Agent 那条
          "模型回了工具调用就判失败"的分支。
        """
        if self._pending_compaction is not None or self._compact_failed:
            return False
        window = self._parent_context_window()
        limit = int(window * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
        if limit <= 0:
            return False
        estimated = self._estimate_context_tokens()
        if estimated <= limit:
            return False
        head_keep = 2
        keep_recent = _keep_recent_tokens(limit, SUBAGENT_COMPACT_KEEP_RECENT_RATIO)
        cut = _find_compaction_cut(self.messages[head_keep:], keep_recent)
        if cut <= 0:
            # 找不到安全切点：别每轮都重算一遍（父 Agent 的 _compact_failed_this_turn 同理）
            self._compact_failed = True
            return False
        cut += head_keep
        # 指令只进内存、不落盘；它也**不会**被写进摘要或报告（收尾时弹掉）
        self.messages.append({"role": "user", "content": SUBAGENT_COMPACT_INSTRUCTION_PROMPT})
        self._pending_compaction = {
            "cut": cut,
            "head_keep": head_keep,
            "estimated": estimated,
            "limit": limit,
        }
        return True

    def _abort_compaction(self) -> None:
        """压缩没成：把那条瞬时指令弹掉，历史回到原样。"""
        if self._pending_compaction is None:
            return
        self._pending_compaction = None
        if self.messages:
            self.messages.pop()

    def _run_compaction_request(self, client: Any, model: str) -> None:
        """发出带压缩指令的那轮请求、收下摘要；失败退回独立摘要请求。

        这轮**不带 tools**（见 _begin_compaction），拿到的正文按 <summary> 解析。整条路子
        （插入式 → 独立请求 → 本地兜底）都不会让子代理卡死。
        """
        pending = self._pending_compaction or {}
        cut = pending.get("cut")
        try:
            content, _, _, _ = self._chat_with_retry(client, model, None)
        except AgentStopRequested:
            self._abort_compaction()  # 父回合被停止：历史不必留着那条指令
            raise
        except Exception as exc:  # noqa: BLE001 - 压缩失败不能拖垮子代理
            _log(f"  ⚠ 子代理 {self.id} 压缩请求失败（{exc}），回退独立摘要请求")
            self._abort_compaction()
            self._compact_via_separate_request(cut)
            return
        if not self._finish_compaction(content):
            _log(f"  ⚠ 子代理 {self.id} 压缩响应里没有摘要，回退独立摘要请求")
            self._compact_via_separate_request(cut)

    def _finish_compaction(self, content: str) -> bool:
        """摘要到手 → 弹掉指令、按切点重建消息列表。"""
        pending = self._pending_compaction
        if pending is None:
            return False
        summary = _parse_compact_summary(content)
        if not summary.strip():
            self._abort_compaction()
            return False
        self._pending_compaction = None
        self.messages.pop()  # 那条压缩指令不是历史
        self._apply_summary(int(pending["head_keep"]), int(pending["cut"]), summary)
        return True

    def _compact_via_separate_request(self, cut: int | None = None) -> None:
        """降级路径：另发一次独立摘要请求（复用父 Agent 的 _summarize_messages，不带 tools）。

        插入式那轮请求失败、或模型没给出摘要时走它；摘要再失败还有 _local_fallback_summary
        收底。切点仍由 _find_compaction_cut 保证 tool_calls 与 tool 响应成对，不会切出非法请求。
        """
        head_keep = 2
        if cut is None:
            limit = int(self._parent_context_window() * COMPACT_TRIGGER_RATIO) - CONTEXT_RESERVE_TOKENS
            keep_recent = _keep_recent_tokens(limit, SUBAGENT_COMPACT_KEEP_RECENT_RATIO)
            cut = _find_compaction_cut(self.messages[head_keep:], keep_recent)
            if cut <= 0:
                self._compact_failed = True
                return
            cut += head_keep
        head = self.messages[head_keep:cut]
        summary = ""
        summarizer = getattr(self.parent, "_summarize_messages", None)
        if callable(summarizer):
            try:
                summary = str(summarizer(head) or "")
            except Exception as exc:  # noqa: BLE001 - 摘要失败必须降级，不能卡死子代理
                _log(f"  ⚠ 子代理 {self.id} 摘要失败，回退本地截断: {exc}")
        if not summary.strip():
            summary = _local_fallback_summary(head)
        self._apply_summary(head_keep, cut, summary)

    def _apply_summary(self, head_keep: int, cut: int, summary: str) -> None:
        """按切点重建消息列表：头部 head_keep 条 + 一条摘要 + 尾部原文。

        被裁掉的旧消息直接丢掉——子代理跑完只交一份报告，中间过程不需要召回（与主 Agent
        的 read_history_archive 不同，它没有"回头查旧账"的需求）。
        """
        dropped = cut - head_keep
        tail = self.messages[cut:]
        self.messages = [
            *self.messages[:head_keep],
            {
                "role": "user",
                "content": (
                    "# 早前工作的压缩摘要\n"
                    f"{summary}\n\n"
                    "以上是之前工作的压缩摘要，请在此基础上继续，不要重复已完成的工作。"
                ),
            },
            *tail,
        ]
        # 子代理的每一步都会推给界面（subagent_message 渲染成一步说明）：压缩这种状态变化
        # 也让它看得见，否则展开子代理会发现步数突然对不上
        self._emit("subagent_message", {
            "round": self.turns,
            "text": f"[上下文压缩] 早前的 {dropped} 条消息已压成摘要（{len(summary)} 字符），从摘要继续。",
        })
        _log(f"  📦 子代理 {self.id} 压缩 {dropped} 条为 {len(summary)} 字符摘要")

    def _chat_with_retry(
        self, client: Any, model: str, tools: list[dict[str, Any]] | None
    ) -> tuple[str, list[Any], str, str]:
        """一次请求 + 与主 Agent 同规则的重试（分类 / 退避 / 可被停止打断）。

        子代理的一次请求就是它整份报告的全部依赖，一次网络抖动就作废太亏，而且这里的
        用量比主请求小得多，重试代价低。规则与主 Agent 一致：只重试瞬态错误（超时 /
        限流 / 5xx / 流连接断了）；鉴权、参数、上下文超限重试多少次都一样，直接失败。
        退避期间父回合被停止就立刻收尾（抛 AgentStopRequested，由 run 转成 stopped）。
        tools=None 是压缩那一轮：不带工具，只求一段文字摘要（见 _begin_compaction）。
        """
        attempt = 0
        while True:
            try:
                return _subagent_chat(client, model, self.messages, tools)
            except Exception as exc:  # noqa: BLE001 - 按分类决定是否重试
                info = _classify_llm_error(exc)
                if self.parent.stop_event.is_set():
                    raise AgentStopRequested() from exc
                if not info["retriable"] or attempt >= LLM_MAX_RETRIES:
                    raise
                attempt += 1
                delay_ms = _llm_retry_delay_ms(attempt, info)
                _log(
                    f"  🧑‍🎓 子代理 {self.id} 请求失败（{info['code']}: {info['message']}），"
                    f"{delay_ms / 1000:g}s 后重试 {attempt}/{LLM_MAX_RETRIES}"
                )
                self._emit("subagent_retry", {
                    "attempt": attempt,
                    "max_attempts": LLM_MAX_RETRIES,
                    "delay_ms": delay_ms,
                    "code": info["code"],
                    "reason": info["message"],
                    "status": info["status"],
                })
                if self.parent.stop_event.wait(delay_ms / 1000):
                    raise AgentStopRequested() from exc

    def run(self) -> dict[str, Any]:
        """跑到自然收尾（不再调工具）、轮数上限、失败或被停止。"""
        client = getattr(self.parent, "_openai_client", None)
        model = str(getattr(self.parent, "_model", "") or "")
        label = SUBAGENT_LABELS.get(self.agent, self.agent)
        self._emit(
            "subagent_start",
            {
                "parent_id": self.parent._active_tool_call_id,
                "agent": self.agent,
                "label": label,
                "file": self.file_label,  # 一组时是「首个 等 N 个文件」，完整清单只在子代理的任务说明里
                "indexes": self.indexes,
                "brief": self.brief,
                "model": model,
                # 开始时间戳（Unix 秒）：subagent_start 是持久事件，刷新/切页后会重放，
                # 界面必须按它算"进行中耗时"，不能拿事件到达时间——否则每次重建都归零。
                "started_at": self.started_at,
            },
        )
        if client is None or not model:
            return self._finish("failed", "", error="主 Agent 的后端还没就绪，子代理起不来")
        self.messages = [
            {"role": "system", "content": self.role.prompt},
            {
                "role": "user",
                "content": self.role.brief.format(
                    label=label,
                    file=self.file_list_text,
                    indexes=self.indexes or "全部",
                    brief=self.brief or "（无）",
                ),
            },
        ]
        tools = _subagent_tools(self.agent)
        # 锁定文件：把它交给 handler，让"只能碰派给自己的那些"由工具层保证
        handlers = _subagent_handlers(self.agent, self.files)
        last_text = ""
        for round_i in range(1, SUBAGENT_MAX_ROUNDS + 1):
            self.turns = round_i
            if self.parent.stop_event.is_set():
                return self._finish("stopped", last_text, error="父回合被停止，子代理提前收尾")
            # 每轮请求前判一次：工具往返堆太多就先压缩——挂上压缩指令、这一轮专门拿摘要
            # （Insert-then-Compress，与父 Agent 同一机制），下一轮带着摘要继续干活。
            if self._begin_compaction():
                self._run_compaction_request(client, model)
                continue
            try:
                content, tool_calls, reasoning_field, reasoning = self._chat_with_retry(
                    client, model, tools
                )
            except AgentStopRequested:
                return self._finish("stopped", last_text, error="父回合被停止，子代理提前收尾")
            except Exception as exc:  # noqa: BLE001 - 子代理失败不该拖垮父回合
                _log(f"  🧑‍🎓 子代理 {self.id} 第 {round_i} 轮请求失败（已重试到上限）: {exc}")
                return self._finish(
                    "failed", last_text, error=f"请求失败（已重试 {LLM_MAX_RETRIES} 次）：{exc}"
                )
            if content.strip():
                last_text = content
                self._emit("subagent_message", {"round": round_i, "text": content[:2000]})
            if not tool_calls:
                return self._finish("done", content or last_text)
            assistant: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if reasoning:
                assistant[reasoning_field] = reasoning
            assistant["tool_calls"] = tool_calls
            self.messages.append(assistant)
            for tool_call in tool_calls:
                self.messages.append(self._run_tool(tool_call, handlers))
        return self._finish(
            "max_rounds", last_text, error=f"轮数到上限（{SUBAGENT_MAX_ROUNDS}），按已有结果收尾"
        )

    def _run_tool(self, tool_call: Any, handlers: dict[str, Any]) -> dict[str, Any]:
        """执行一次工具调用（白名单之外一律拒绝），返回要塞回消息历史的那条 tool 消息。"""
        call_id = str(getattr(tool_call, "id", "") or "")
        fn = getattr(tool_call, "function", None)
        name = str(getattr(fn, "name", "") or "")
        raw = str(getattr(fn, "arguments", "") or "")
        self.tool_calls += 1
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {}
        args = parsed if isinstance(parsed, dict) else {}
        self._emit(
            "subagent_tool_call",
            {"tool_call_id": call_id, "name": name, "arguments": _sanitize_tool_args(args)},
        )
        handler = handlers.get(name)
        started = time.time()
        ok = True
        result: Any = None
        try:
            if handler is None:
                raise AgentToolError(
                    f"子代理没有这个工具：{name}（只能用 {'、'.join(self.role.tools)}）"
                )
            result = handler(self.parent, args)
            # 子代理读缓存是大头（校对一批 16 个、每个几十条）：同样走 Markdown 渲染
            rendered = _render_tool_result_table(name, result)
            payload = rendered if rendered is not None else json.dumps(result, ensure_ascii=False)
        except AgentToolError as exc:
            payload, ok = str(exc), False
        except Exception as exc:  # noqa: BLE001
            payload, ok = f"{type(exc).__name__}: {exc}", False
        duration_ms = int((time.time() - started) * 1000)
        if ok and name == "patch_transl_cache":
            self._remember_proofread_comments(result, str(args.get("filename", "") or ""))
        # 事件里只给预览（读缓存动辄几万字符，界面用不上）；消息历史里给全文（有上限兜底）
        self._emit(
            "subagent_tool_result",
            {
                "tool_call_id": call_id,
                "name": name,
                "ok": ok,
                "result": payload[:400] if ok else None,
                "error": None if ok else payload[:400],
                "duration_ms": duration_ms,
            },
        )
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _truncate_text(
                payload,
                SUBAGENT_TOOL_RESULT_CHARS,
                "…（结果过长已截断：请用 index 区间分段读，别跳过没读的条目）",
            ),
        }

    def _remember_proofread_comments(self, result: Any, filename: str) -> None:
        """从 patch_transl_cache 的变更里挑出 proofread_comment 那几条，记进报告用的小结。

        认的是返回的 changes 而不是模型传的参数：它到底写了什么、写没写成功，以工具的返回为准。
        文件取变更行上的 file（一次调用可以跨文件，见 _patch_one_cache_file），取不到才退回
        调用方给的 filename——一个子代理可能负责一组文件，只留 index 主 Agent 认不出在哪份文件里。
        """
        if not isinstance(result, dict):
            return
        for change in result.get("changes") or []:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "")
            if not path.endswith(".proofread_comment"):
                continue
            # path 形如 `#33.proofread_comment`，跨文件批量时带「文件名#」前缀——
            # 所以从后往前取，别被文件名里的点切错。
            raw = path.rsplit(".", 1)[0].rsplit("#", 1)[-1]
            try:
                index: Any = int(raw)
            except ValueError:
                index = raw
            self.proofread_comments.append(
                {
                    "file": str(change.get("file") or filename),
                    "index": index,
                    "content": str(change.get("after") or ""),
                }
            )


def _split_summary(agent: str, total: int, parts: int, sizes: list[int]) -> str:
    """自动均分后给主 Agent 的一句交代（分了多少、每个多少、有没有任务被跳过）。"""
    label = SUBAGENT_LABELS.get(agent, agent)
    non_empty = [size for size in sizes if size]
    span = str(non_empty[0]) if len(set(non_empty)) == 1 else f"{min(non_empty)}-{max(non_empty)}"
    text = f"已把 {total} 个文件自动均分给 {parts} 个「{label}」子代理（每个 {span} 个）"
    skipped = parts - len(non_empty)
    if skipped:
        text += f"；有 {skipped} 个任务没分到文件，已跳过（少派一个就少烧一份 token）"
    return text + "。"


def _tool_run_subagents(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """派一批子代理并行干活，等它们全部跑完，把每份报告收回来。

    三个维度互相独立（正交），任意组合：
    - **选谁（file）**：选择器 → 有序文件清单。具体文件名 / "*"（全部，自动均分）/
      "list:a.json,b.json" / "glob:SW_01_*" / "regex:^0[12]_" / "select:has_problem" /
      "select:problem_type=残留日文"（吃 list_problems 的结果集）/ "random:N"（随机 N 个）。
    - **切几份（count）**：对任何选择器都生效——文件够就按文件均分；文件不够就把大文件按
      index 切成 count 段（一个大文件切给 N 个代理并行）。
    - **取哪段（indexes）**：单文件 + count>1 时在区间内再切段；多文件 + indexes 判为歧义。
    文件是任务的天然边界——不同子代理写不同文件的 proofread_comment 不会互相覆盖；同一个
    文件要拆就用切片（count>1 或 indexes），但两边别碰同一条。
    原文探索的 file 可以留空（自己按 list_input_files 挑）：它只读、不写文件。
    """
    tasks_raw = args.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise AgentToolError("tasks 必须是非空数组")
    if len(tasks_raw) > SUBAGENT_MAX_TASKS:
        raise AgentToolError(
            f"一次最多派 {SUBAGENT_MAX_TASKS} 个子代理（收到 {len(tasks_raw)} 个）：拆成两次调用。"
        )

    # 第一遍：只校验入参、把每条任务解析成（选择器 × count × indexes），暂不展开
    parsed: list[dict[str, Any]] = []
    for i, item in enumerate(tasks_raw, start=1):
        if not isinstance(item, dict):
            raise AgentToolError(f"第 {i} 个任务不是对象")
        agent = str(item.get("agent", "") or "").strip()
        if agent not in SUBAGENT_AGENTS:
            raise AgentToolError(
                f"第 {i} 个任务的 agent 不认识：{agent!r}（可用：{'、'.join(SUBAGENT_AGENTS)}）"
            )
        spec = str(item.get("file", "") or "").strip()
        if SUBAGENT_ROLES[agent].needs_file and not spec:
            raise AgentToolError(
                f"第 {i} 个任务缺 file：{SUBAGENT_LABELS.get(agent, agent)} 要锁定一个缓存文件"
                f'（也可以填 "{SUBAGENT_FILE_ALL}" 让它自动均分一批）'
            )
        # count：把这一条任务展开成几个子代理并行跑。brief 只写一遍——不让模型为了并行
        # 把上千字的 brief 复制 N 份，否则它宁可只派一个（真实踩过：要求派 2 个 explore，
        # 模型因为不想重复长 brief 只写了一条 task）。
        count_raw = item.get("count", 1)
        if isinstance(count_raw, bool) or not isinstance(count_raw, (int, str)):
            raise AgentToolError(f"第 {i} 个任务的 count 必须是整数")
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise AgentToolError(f"第 {i} 个任务的 count 必须是整数（收到 {count_raw!r}）") from exc
        if count < 1:
            raise AgentToolError(f"第 {i} 个任务的 count 至少是 1")
        if count > SUBAGENT_MAX_TASKS:
            raise AgentToolError(
                f"第 {i} 个任务的 count 最多 {SUBAGENT_MAX_TASKS}（收到 {count}）"
            )
        if not spec and count > 1:
            raise AgentToolError(
                f"第 {i} 个任务的 file 是空的（让子代理自己挑），没法平分给 {count} 个子代理："
                "给 file 一个选择器，或把 count 去掉。"
            )
        parsed.append(
            {
                "index": i,
                "agent": agent,
                "spec": spec,
                "count": count,
                "indexes": str(item.get("indexes", "") or "").strip(),
                "brief": str(item.get("brief", "") or "").strip(),
            }
        )

    counts_cache: dict[str, dict[str, int]] = {}

    def _counts(agent: str) -> dict[str, int]:
        if agent not in counts_cache:
            counts_cache[agent] = _subagent_file_counts(runner, agent)
        return counts_cache[agent]

    # 先解析"具体清单"类选择器：它们选中的文件记下来，供 "*" 排除（点名优先，避免抢同一份）
    named: dict[str, set[str]] = {}
    for task in parsed:
        spec = task["spec"]
        if spec == "" or spec == SUBAGENT_FILE_ALL:
            continue
        task["files"] = _resolve_subagent_files(
            runner, task["agent"], spec, list(_counts(task["agent"]))
        )
        if not task["files"]:
            raise AgentToolError(
                f"第 {task['index']} 个任务的 file 选择器（{spec}）一个文件都没选中："
                "先用 read_transl_cache（action=list）/ list_problems 看看有哪些文件。"
            )
        named.setdefault(task["agent"], set()).update(task["files"])

    # "*"：本批里同角色所有 "*"（含 count 展开的份数）平分"全部候选 - 已点名"
    auto_slots: dict[str, list[dict[str, Any]]] = {}
    for task in parsed:
        if task["spec"] == SUBAGENT_FILE_ALL:
            auto_slots.setdefault(task["agent"], []).append(task)
    split_notes: list[str] = []
    auto_groups: dict[str, list[list[str]]] = {}
    for agent, slots in auto_slots.items():
        pool = [name for name in _counts(agent) if name not in named.get(agent, set())]
        if not pool:
            hint = (
                "缓存里还没有可校对的文件（条目为空或还没跑翻译）：先用 read_transl_cache（action=list）看看，"
                "或先把具体文件名写出来。"
                if agent == SUBAGENT_AGENT_PROOFREAD
                else "输入目录里没有可分派的原文文件：先用 list_input_files 看看。"
            )
            raise AgentToolError(f"「{SUBAGENT_LABELS.get(agent, agent)}」自动均分拿不到文件：{hint}")
        total_slots = sum(task["count"] for task in slots)
        groups = _split_files_evenly(pool, total_slots)
        auto_groups[agent] = groups
        split_notes.append(_split_summary(agent, len(pool), total_slots, [len(g) for g in groups]))

    # 第二遍：按模型写的顺序逐条展开成最终任务（顺序稳定，方便对账）
    tasks: list[dict[str, Any]] = []

    def _emit_task(source: dict[str, Any], files: list[str], indexes: str) -> None:
        tasks.append(
            {
                "agent": source["agent"],
                "files": list(files),
                "auto": source["spec"] == SUBAGENT_FILE_ALL,
                "indexes": indexes,
                "brief": source["brief"],
            }
        )

    for task in parsed:
        spec = task["spec"]
        if spec == SUBAGENT_FILE_ALL:
            groups = auto_groups[task["agent"]]
            for _ in range(task["count"]):
                _emit_task(task, groups.pop(0), task["indexes"])
        elif spec == "":
            _emit_task(task, [], "")
        else:
            expansions = _expand_subagent_file_selection(
                task["files"], task["indexes"], task["count"], _counts(task["agent"])
            )
            if task["count"] > 1:
                split_notes.append(
                    _selection_split_note(task["agent"], task["files"], expansions, task["count"])
                )
            for files, indexes in expansions:
                _emit_task(task, files, indexes)

    if len(tasks) > SUBAGENT_MAX_TASKS:
        raise AgentToolError(
            f"展开 count 后一次要派 {len(tasks)} 个子代理，超过上限 {SUBAGENT_MAX_TASKS}："
            "调小 count，或分两次调用。"
        )

    # 没分到文件的任务直接丢掉（空跑一轮照样烧 token），但"file 留空"的探索任务照旧派出
    scheduled = [task for task in tasks if task["files"] or not task["auto"]]
    skipped = len(tasks) - len(scheduled)
    tasks = scheduled

    if getattr(runner, "_openai_client", None) is None or not str(getattr(runner, "_model", "") or ""):
        raise AgentToolError("本回合的后端还没就绪，子代理跑不起来")

    slots: list[dict[str, Any] | None] = [None] * len(tasks)
    lock = threading.Lock()
    base = os.urandom(8).hex()

    def work(slot: int, task: dict[str, Any]) -> None:
        try:
            sub = SubAgentRunner(
                runner,
                agent=task["agent"],
                files=task["files"],
                indexes=task["indexes"],
                brief=task["brief"],
                delegation_id=f"{base}-{slot + 1:02d}",
            )
            out = sub.run()
        except Exception as exc:  # noqa: BLE001 - 子代理自己崩了只影响它这一格
            _log(f"  🧑‍🎓 子代理 {slot + 1} 起不来或崩了: {exc}")
            out = {
                "agent": task["agent"],
                "label": SUBAGENT_LABELS.get(task["agent"], task["agent"]),
                "file": "、".join(task["files"][:1]) + (
                    f" 等 {len(task['files'])} 个文件" if len(task["files"]) > 1 else ""
                ),
                "files": list(task["files"]),
                "indexes": task["indexes"],
                "status": "failed",
                "report": "",
                "turns": 0,
                "tool_calls": 0,
                "proofread_comment": [],
                "duration_ms": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        with lock:
            slots[slot] = out

    threads = [
        threading.Thread(target=work, args=(i, task), name=f"subagent-{base}-{i + 1}", daemon=True)
        for i, task in enumerate(tasks)
    ]
    def _dispatch_label(task: dict[str, Any]) -> str:
        """日志里每个子代理的叫法：一组文件是「首个 等 N 个」，没分配文件的就是角色名。"""
        if len(task["files"]) > 1:
            return f"{task['files'][0]} 等 {len(task['files'])} 个文件"
        if task["files"]:
            return str(task["files"][0])
        return str(SUBAGENT_LABELS.get(task["agent"], task["agent"]))

    _log(
        f"  🧑‍🎓 派出 {len(threads)} 个子代理："
        + "、".join(_dispatch_label(task) for task in tasks)
    )
    for thread in threads:
        thread.start()
    started = time.time()
    while any(thread.is_alive() for thread in threads):
        if runner.stop_event.is_set():
            # 停止信号：各子代理在自己的轮次边界退出，这里不再死等
            _log("  🧑‍🎓 父回合被停止，等待子代理收尾")
            for thread in threads:
                thread.join(timeout=2.0)
            break
        # 以 0.1s 为步长轮询，而不是直接 sleep(5)：子代理一跑完就收尾，别让父回合
        # 白等一个 tick——只派一个、或最后一个刚跑完时，那 5 秒是纯浪费。
        # SUBAGENT_PROGRESS_TICK 只用来控制进度日志的节奏。
        deadline = time.time() + SUBAGENT_PROGRESS_TICK
        while time.time() < deadline and any(t.is_alive() for t in threads):
            if runner.stop_event.is_set():
                break
            time.sleep(0.1)
        if not any(thread.is_alive() for thread in threads):
            break
        alive = sum(1 for thread in threads if thread.is_alive())
        _log(f"  🧑‍🎓 子代理并行中：还剩 {alive}/{len(threads)} 个（已 {int(time.time() - started)}s）")

    results: list[dict[str, Any]] = []
    for task, out in zip(tasks, slots):
        if out is None:
            out = {
                "agent": task["agent"],
                "label": SUBAGENT_LABELS.get(task["agent"], task["agent"]),
                "file": "、".join(task["files"][:1]) + (
                    f" 等 {len(task['files'])} 个文件" if len(task["files"]) > 1 else ""
                ),
                "files": list(task["files"]),
                "indexes": task["indexes"],
                "status": "stopped",
                "report": "",
                "turns": 0,
                "tool_calls": 0,
                "proofread_comment": [],
                "duration_ms": 0,
                "error": "父回合被停止，这个子代理没跑完",
            }
        results.append(out)
    total_comments = sum(len(row.get("proofread_comment") or []) for row in results)
    note = (
        "子代理的校对意见已写进各条缓存的 proofread_comment：按各子代理批注表里的 file 与 index "
        "用 read_transl_cache 读那些条目，改完译文（pre_dst）后再用 patch_transl_cache 把该条的 "
        "proofread_comment 清空，表示已处理。"
    )
    if total_comments == 0:
        note = "这批子代理没有提出任何疑问（没有条目被写入 proofread_comment）。"
    if split_notes:
        note = " ".join(split_notes) + " " + note
    return {
        "tasks": results,
        "total": len(results),
        "skipped": skipped,
        "total_proofread_comment": total_comments,
        "note": note,
    }
