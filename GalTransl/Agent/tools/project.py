"""项目类工具：项目概览、配置读写、项目翻译规范。"""

from __future__ import annotations

import copy
import re
import urllib.parse
from typing import Any, TYPE_CHECKING

from GalTransl.Agent.core import DEFAULT_CONFIG_FILE
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.common import _change, _diff_lines
from GalTransl.Agent.tools.plugin_settings import catalog_for_updates, set_plugin_value, validate_plugin_value

if TYPE_CHECKING:
    from GalTransl.Agent.runner import AgentRunner


# ---- 工具实现 ----
# 配置键说明表：get_project_overview 返回配置时附上，让 Agent 看懂每个键的
# 作用与取值。口径与 sampleProject/config.inc.yaml 的注释、桌面端「项目配置」
# 页各 section 的字段说明一致。键用点号路径，与 YAML 展平后一致。
CONFIG_FIELD_DESCRIPTIONS: dict[str, str] = {
    # ---- common ----
    "common.gpt.numPerRequestTranslate": "每次请求打包的句子数，建议不超过 16 [1-32]",
    "common.workersPerProject": "项目级并行文件数；单文件并行需配合 splitFile",
    "common.autoAdjustWorkers": "根据近期 429 比例和响应延迟自动降/升 worker 并发 [true/false]",
    "common.sortBy": "文件调度顺序：name 按文件名，size 优先大文件（并行时通常更快）",
    "common.language": "目标输出语言 [zh-cn/zh-tw/en/ja/ko/ru/fr]",
    "common.splitFile": "单文件分片模式：no 关闭；Num 每 n 句切一片；Equal 每文件均分 n 片。【重要】分割设置直接影响缓存读取命中，迁移旧项目必须保持一致",
    "common.splitFileNum": "分片参数：Num 模式表示每片句数；Equal 模式表示分片总数",
    "common.splitFileCrossNum": "分片重叠句数（上下文缓冲），可提升片段衔接质量 [常用 0 或 10]",
    "common.save_steps": "每处理 n 个批次保存一次缓存；值越大保存越少、速度可能更快",
    "common.start_time": "定时启动时间（24 小时制，如 00:30）；留空立即启动",
    "common.linebreakSymbol": "JSON 内换行符类型，供问题检测/自动修复使用，不改变翻译语义",
    "common.skipH": "是否跳过可能触发敏感词检测的句子 [true/false]",
    "common.smartRetry": "解析失败时自动缩小批次并重置上下文，减少无效重试 [true/false]",
    "common.retranslFail": "程序重启时是否自动重翻标记为 (Failed) 的句子 [true/false]",
    "common.retranslKey": "重翻关键字列表：启动时命中缓存 problem 或原文关键字的句子会被重翻（如「翻译失败」「残留日文」）",
    "common.problemFilterKey": "问题过滤关键字列表：**正则列表**，每项是一条正则，命中的问题项在问题统计与 list_problems 中被过滤掉。原则上只过滤小类（如 `缺失.*标点`、`^残留日文：♪`），不要用 `残留日文` 这类整类写法",
    "common.problemWhiteList": "问题白名单：按「缓存文件名:index」（如 a.json:12，区间写 a.json:12-15）豁免指定缓存条目的问题，等价于给该条勾选 skip_check",
    "common.gpt.contextNum": "每次请求附带的前文句数；值越大上下文越强、成本越高（常用 8）[0-32]",
    "common.gpt.translation_guideline": "使用的**全局**翻译规范文件名（位于 translation_guidelines 文件夹），决定文风与措辞；项目专属规范不是配置项，而是项目目录里的 translation_guideline.md（用 read_guideline/write_project_guideline 读改），翻译时拼在全局规范之后",
    "common.gpt.enhance_jailbreak": "是否启用「抗拒答」增强提示，降低模型拒答概率 [true/false]",
    "common.gpt.token_limit": "(Sakura/GalTransl) 单轮 token 上限；0 表示不限制，用于避免上下文溢出",
    "common.loggingLevel": "日志输出级别：debug 详细，info 常规，warning 仅警告 [debug/info/warning]",
    "common.saveLog": "是否将运行日志写入文件 [true/false]",
    "common.gpt.dynamicNumPerRequestTranslate": "动态句数调整：根据模型解析错误自动降/升单次翻译句数 [true/false]",
    # ---- problemAnalyze ----
    "problemAnalyze.problemList": "要启用的问题检测清单（词频过高/标点错漏/残留日文/丢失换行/多加换行/比日文长/比日文长严格/字典使用/引入英文/语言不通/缺控制符/独白男他/单句过长）",
    "problemAnalyze.avgSentenceLengthThreshold": "单句过长检测的平均分句长度阈值",
    "problemAnalyze.arinashiDict": "有無字典：检测多加/漏加字典符号（如【】）的词表",
    # ---- dictionary ----
    "dictionary.defaultDictFolder": "通用字典文件夹（相对程序目录，也可绝对路径）",
    "dictionary.usePreDictInName": "将译前字典用在 name 字段（人名替换）[true/false]",
    "dictionary.usePostDictInName": "将译后字典用在 name 字段 [true/false]",
    "dictionary.useGPTDictInName": "将 GPT 字典用在 name 字段 [true/false]",
    "dictionary.sortDict": "将所有字典按查找词长度重排序 [true/false]",
    "dictionary.preDict": "译前字典文件列表（每行一个；前缀 (project_dir) 代表在项目目录下）。译前字典在送入模型前直接替换原文",
    "dictionary.gpt.dict": "GPT 字典文件列表。随 Prompt 发给模型，约束人名/术语译法（Agent 应主要维护这层）",
    "dictionary.postDict": "译后字典文件列表。翻译完成后对译文做替换（符号矫正等）",
    # ---- plugin ----
    "plugin.filePlugin": "文件插件（决定输入/输出格式）：auto 按每个文件自动识别（gt_input 可混放多种格式）；file_galtransl_json；字幕 file_subtitle_srt_lrc_vtt；小说 file_epub_epub / file_plaintext_txt；Mtool json 用 file_i18n_json",
    "plugin.textPlugins": "文本处理插件列表（按顺序执行）：如 text_common_normalfix 常规修复、text_common_skipNoJP 跳过无日文句",
    # ---- proxy ----
    "proxy.enableProxy": "是否启用代理 [true/false]，使用中转供应商时一般不用开",
}


def _annotate_config(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """给配置附带键说明。只附配置里实际存在的键（含点号键的 section 形式，
    如 common 里直接写 gpt.numPerRequestTranslate 的展平路径）。"""
    section_hints = {
        "common": "通用程序设置",
        "problemAnalyze": "自动问题分析配置",
        "dictionary": "字典设置",
        "plugin": "文件/文本插件配置",
        "proxy": "代理设置",
    }
    descriptions: dict[str, str] = {}
    for section, note in section_hints.items():
        if section in config:
            descriptions[section] = note
    for dotted, desc in CONFIG_FIELD_DESCRIPTIONS.items():
        if "." not in dotted:
            continue
        section, _, key = dotted.partition(".")
        value = config.get(section)
        if isinstance(value, dict) and key in value:
            descriptions[dotted] = desc
    return config, descriptions


# 已下线的旧 Prompt 键（common 下的展平键名）：只有老工程配置文件里还留着，
# 翻译流程仍认它们（BaseTranslate 的兼容分支），但不再提供给前端与 Agent——
# get_project_overview 不返回、update_project_config 直接拒掉，模型就不会去碰。
# 现在的自定义入口是项目翻译规范（ProjectGuideline / write_project_guideline）。
LEGACY_PROMPT_KEYS = ("gpt.change_prompt", "gpt.prompt_content")
LEGACY_PROMPT_PATHS = frozenset(f"common.{key}" for key in LEGACY_PROMPT_KEYS)


# 翻译进行中会在 <缓存>.json 旁并行写 <缓存>.json.append.jsonl 增量日志（见 GalTransl/Cache.py）
_APPEND_CACHE_SUFFIX = ".append.jsonl"


def _input_cache_matchers(name: str) -> tuple[set[str], re.Pattern[str]]:
    """把输入文件名换算成它在缓存目录里可能出现的文件名，用于把缓存归属回输入文件。

    命名规则（见 Frontend/LLMTranslate._build_runtime_file_maps 与 doLLMTranslSingleChunk）：
    输入文件相对路径把分隔符替换成 "-}"，多分块再追加 "_<分块号>"，最后
    save_transCache_to_json 在结尾补一次 ".json"（已经以 ".json" 结尾则不再补）。例如：
      foo.json → foo.json（单块）/ foo.json_0.json（多块）
      foo.ks   → foo.ks.json（单块）/ foo.ks_0.json（多块）
    """
    base = name.replace("/", "-}").replace("\\", "-}")
    single = base if base.endswith(".json") else f"{base}.json"
    singles = {single, f"{single}{_APPEND_CACHE_SUFFIX}"}
    chunk_re = re.compile(rf"^{re.escape(base)}_\d+\.json(?:{re.escape(_APPEND_CACHE_SUFFIX)})?$")
    return singles, chunk_re


def _count_input_file_progress(input_files: list[str], progress_files: list[dict[str, Any]]) -> dict[str, int]:
    """按输入文件统计「已有译文的文件数 / 尚无译文的文件数」。

    progress_files 是 /progress 返回的 files（每个缓存文件的 filename 与 translated 句数）。
    只有累计译文句数 > 0 的文件才算「已翻译」，这样即使 rebuild 阶段生成了全空缓存，
    也不会把尚未翻译的文件误算成已翻译。
    """
    translated_by_cache = {
        str(item.get("filename", "")): int(item.get("translated", 0) or 0)
        for item in progress_files
        if isinstance(item, dict) and item.get("filename")
    }
    files_translated = 0
    for name in input_files:
        singles, chunk_re = _input_cache_matchers(name)
        translated = sum(
            count
            for cache_name, count in translated_by_cache.items()
            if cache_name in singles or chunk_re.match(cache_name)
        )
        if translated > 0:
            files_translated += 1
    total = len(input_files)
    return {
        "files_total": total,
        "files_translated": files_translated,
        "files_untranslated": max(total - files_translated, 0),
    }


def _config_for_overview(raw: Any) -> dict[str, Any]:
    """「了解项目」返回的配置快照：剔除 backendSpecific 与已下线的旧键。

    backendSpecific 那节是 API 令牌、端点等敏感信息（发给模型等于把密钥递出去），
    对"了解项目"也没有价值——实际生效的后端见返回里的 backend 字段。

    LEGACY_PROMPT_KEYS 同理不给模型看：它们只为老工程保留（翻译流程仍认），模型看不到
    就不会去改；新项目不会再生成这两个键。
    """
    if not isinstance(raw, dict):
        return {}
    out = {k: v for k, v in raw.items() if k != "backendSpecific"}
    common = out.get("common")
    if isinstance(common, dict):
        out["common"] = {k: v for k, v in common.items() if k not in LEGACY_PROMPT_KEYS}
    return out


def _backend_summary(profile: Any, name: str = "") -> dict[str, str]:
    """一份后端配置 → {name, type, model}：只给名字与模型名，不含地址与密钥。"""
    section = ""
    model = ""
    if isinstance(profile, dict):
        for key, conf in profile.items():
            if not isinstance(conf, dict):
                continue
            section = str(key)
            if key == "OpenAI-Compatible":
                tokens = conf.get("tokens")
                if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict):
                    model = str(tokens[0].get("modelName") or "")
            break
    return {"name": name or section, "type": section, "model": model}


def _backend_overview(runner: AgentRunner) -> dict[str, Any]:
    """实际生效的两份后端：本会话（Agent）用的 + 翻译任务会用的。

    项目配置文件里的 backendSpecific 常是旧值（后端还会被全局后端配置覆盖），
    所以两份都以"真正会被使用"的配置为准：
    - agent：runner 手里那份（Agent 自己这一会话用的）；
    - translator：前端送来的项目选择（没有项目选择时就是全局"翻译器默认"）——
      启动翻译任务用的就是它（见 _tool_start_translation）。
    """
    state = runner.state
    return {
        "agent": _backend_summary(state.backend_profile_data, state.backend_profile_name),
        "translator": _backend_summary(
            state.translator_profile_data, state.translator_profile_name
        ),
    }


# 「了解项目」可分段返回。名字与返回体的键一一对应（include 里写什么，回来就是什么键），
# 顺序即返回顺序；不传 include 就是全部（保持老行为）。
OVERVIEW_SECTIONS: tuple[str, ...] = (
    "progress",
    "backend",
    "config",
    "config_field_descriptions",
)


def _normalize_overview_include(args: dict[str, Any]) -> list[str]:
    """校验 include：不传 = 全部；传了就去重并按标准顺序返回，未知名字直接报错。"""
    raw = args.get("include")
    if raw is None:
        return list(OVERVIEW_SECTIONS)
    if not isinstance(raw, list) or not raw:
        raise AgentToolError("include 必须是非空数组（不传表示返回全部）")
    wanted: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if name not in OVERVIEW_SECTIONS:
            raise AgentToolError(
                f"include 里有未知的部分：{name}（可选：{'、'.join(OVERVIEW_SECTIONS)}）"
            )
        wanted.add(name)
    if not wanted:
        raise AgentToolError(f"include 里没有有效部分（可选：{'、'.join(OVERVIEW_SECTIONS)}）")
    return [section for section in OVERVIEW_SECTIONS if section in wanted]


def _tool_get_project_overview(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """了解项目。按 include 分块返回（不传 = 全部）。

    分块的意义：config 与 config_field_descriptions（约 40 个键的说明）基本是静态的，
    开局拿全看过一次之后，再查进度时没有理由原样重发一遍。只取需要的部分，没要配置
    就**连那条 HTTP 都不发**（省一次本机往返），返回体也不会被那份静态说明撑大。
    """
    include = _normalize_overview_include(args)
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    cfg_name = urllib.parse.quote(config_name)
    out: dict[str, Any] = {}

    if "progress" in include:
        progress = runner._http_get(f"/api/projects/{pid}/progress?config={cfg_name}")
        files = runner._http_get(f"/api/projects/{pid}/files")
        input_files = [
            str(entry.get("name", ""))
            for entry in files.get("input_files", [])
            if isinstance(entry, dict) and entry.get("is_file", True) and entry.get("name")
        ]
        file_counts = _count_input_file_progress(input_files, progress.get("files", []))
        out["progress"] = {
            "total": progress.get("total", 0),
            "translated": progress.get("translated", 0),
            "problems": progress.get("problems", 0),
            "failed": progress.get("failed", 0),
            **file_counts,
            "note": (
                "total/translated 是句数，且只统计已生成缓存的文件；未开始翻译的文件不计入分母，"
                "所以 translated==total 只说明「已有缓存的部分翻完了」，不代表整个项目翻完。"
                "整体进度请结合 files_translated/files_total 判断。"
                "这里是**已落盘缓存**的口径（扫缓存目录得到）；本轮任务自身的计数与 ETA 见 "
                "get_runtime 的 summary（按任务计划统计，含正在翻译、尚未落盘的文件，"
                "total 通常比这里大）——两个分母不同，别拿它们互相校对。"
            ),
        }

    if "backend" in include:
        out["backend"] = {
            **_backend_overview(runner),
            "note": (
                "实际生效的后端（各自含 name 配置名 / type 后端类型 / model 模型名）："
                "agent 是本 Agent 会话在用的；translator 是翻译任务会用的"
            ),
        }

    if "config" in include or "config_field_descriptions" in include:
        cfg = runner._http_get(f"/api/projects/{pid}/config?config={cfg_name}")
        config, descriptions = _annotate_config(_config_for_overview(cfg.get("config")))
        if "config" in include:
            out["config"] = config
        if "config_field_descriptions" in include:
            out["config_field_descriptions"] = descriptions

    if len(include) < len(OVERVIEW_SECTIONS):
        out["note"] = (
            f"本次只返回了{'、'.join(include)}；需要其它部分时再调用一次并带上对应的 include"
            f"（可选：{'、'.join(OVERVIEW_SECTIONS)}）。"
        )
    return out


def _tool_read_guideline(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """读取翻译规范：scope=global 读全局规范库（不带 name 列出文件名），scope=project 读项目规范。"""
    scope = str(args.get("scope", "") or "global").strip().lower()
    if scope == "project":
        return runner._http_get(f"/api/projects/{runner._project_id()}/guideline")
    name = str(args.get("name", "") or "").strip()
    if not name:
        data = runner._http_get("/api/translation-guidelines")
        guidelines = data.get("guidelines", [])
        current = "（见 get_project_overview 配置 common.gpt.translation_guideline）"
        return {
            "guidelines": guidelines,
            "note": (
                f"当前项目使用的全局规范：{current}。传 name 读取全文；"
                '本项目专属的项目规范用 scope="project" 读。'
            ),
        }
    return runner._http_get(f"/api/translation-guidelines/{urllib.parse.quote(name)}")


def _tool_write_project_guideline(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """写项目规范（overwrite 覆写 / append 增写 / replace 替换）。

    三种模式的实现都在后端（server → ProjectGuideline.apply_project_guideline_edit）：
    replace 没命中或命中多处会返回 400，由 _http_json 转成工具错误原样给模型看，
    这里不做二次加工，免得"到底改成了什么"有两套口径。

    行级 diff（前端渲染「变更」卡的依据）也按同一原则来：**写前、写后各读一次文件**，
    diff 的是真正落盘的内容，而不是在本地按三种模式重算一遍编辑结果——后者等于把
    "改成什么样"的逻辑实现第二遍，早晚跟后端那份对不上。

    审批卡上的提前预览同理，只是那次带 dry_run（见 _preview_guideline_write）：同一个入口，
    服务端算完就走。所以"批准前看到的 diff"和"批准后落盘的内容"来自同一段拼接逻辑。
    """
    endpoint = f"/api/projects/{runner._project_id()}/guideline"
    before = str((runner._http_get(endpoint) or {}).get("content") or "")
    body = {
        "mode": str(args.get("mode", "") or "").strip(),
        "content": str(args.get("content", "") or ""),
        "old_text": str(args.get("old_text", "") or ""),
        "new_text": str(args.get("new_text", "") or ""),
    }
    result = runner._http_put(endpoint, body)
    after = str((runner._http_get(endpoint) or {}).get("content") or "")

    diff = _diff_lines(before, after)
    added = diff["added"]
    removed = diff["removed"]
    out: dict[str, Any] = dict(result) if isinstance(result, dict) else {}
    out.update(
        {
            "changed": before != after,
            "lines_before": len(before.splitlines()),
            "lines_after": len(after.splitlines()),
            "lines_added": added,
            "lines_removed": removed,
            "line_diff": diff,
        }
    )
    return out


def _parse_config_value(raw: Any) -> Any:
    """把工具参数里的标量值转成 YAML 配置里应有的类型。

    模型经 JSON 传参，bool/数字天然带类型；字符串保持字符串——
    YAML 里本来就大量存在如 "Num"/"size" 的字符串枚举，不做猜测。"""
    return raw


def _set_nested(config: dict[str, Any], dotted: str, value: Any) -> bool:
    """按点号路径写入配置（如 common.gpt.contextNum）。返回键是否原本存在。"""
    parts = dotted.split(".")
    node: Any = config
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return False
        node = node[p]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    node[parts[-1]] = value
    return True


# 点号键的特殊展平：YAML 里 common 下可以直接写 "gpt.numPerRequestTranslate"
# 这种带点的键（不嵌套），但也存在真正的嵌套（如 common.gpt 为一个 dict）。
# 匹配顺序：section 内字面点号键 → section 内短键 → 整体嵌套路径。
def _set_config_key(config: dict[str, Any], dotted: str, value: Any) -> bool:
    sections = ("common", "problemAnalyze", "dictionary", "plugin", "proxy", "backendSpecific")
    section, _, rest = dotted.partition(".")
    if section in sections and rest:
        node = config.get(section)
        if isinstance(node, dict):
            # 字面点号键（common 的展平写法）
            if dotted in node:
                node[dotted] = value
                return True
            # 短键（去掉 section 前缀后直接是键名）
            if rest in node:
                node[rest] = value
                return True
            # 真嵌套（section 下有同名子 dict），交给通用嵌套写入
            if isinstance(node.get(rest.split(".")[0]), dict):
                return _set_nested(config, dotted, value)
            return False
    return _set_nested(config, dotted, value)


# _get_config_key 的「键不存在」哨兵（None 和 False 都是合法配置值，不能用）
_MISSING = object()


def _get_config_key(config: dict[str, Any], dotted: str) -> Any:
    """按 _set_config_key 的同一套匹配顺序读配置值。键不存在返回 _MISSING。"""
    sections = ("common", "problemAnalyze", "dictionary", "plugin", "proxy", "backendSpecific")
    section, _, rest = dotted.partition(".")
    if section in sections and rest:
        node = config.get(section)
        if isinstance(node, dict):
            if dotted in node:
                return node[dotted]
            if rest in node:
                return node[rest]
            if isinstance(node.get(rest.split(".")[0]), dict):
                # 复用 _set_nested 的路径遍历
                parts = dotted.split(".")
                cur: Any = config
                for p in parts:
                    if not isinstance(cur, dict) or p not in cur:
                        return _MISSING
                    cur = cur[p]
                return cur
            return _MISSING
    parts = dotted.split(".")
    cur: Any = config
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _plan_config_updates(
    config: dict[str, Any], updates: list[Any], plugin_catalog: dict | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """把 updates 解析成「要改哪些键、改成什么」（**只读**：在副本上算，不动传入的 config）。

    update_project_config 的落盘与审批卡上的「将要变更」预览共用这一份判断：卡上显示的
    before→after 就是真执行会写下去的东西。在**副本**上跑一遍 _set_config_key，而不是另写
    一套"这个键存不存在"的判断——展开的点号键 / 短键 / 真嵌套（见 _set_config_key）那套
    匹配顺序只有一处实现，连"同一个键被改两次时第二条的 before"这种细节也一致。
    返回（applied, skipped, changes）。
    """
    probe = copy.deepcopy(config)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for item in updates:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        if key in LEGACY_PROMPT_PATHS:
            # 旧工程里这两个键还在（翻译流程仍认），但已不对外提供：直接拒掉，
            # 免得模型绕开 write_project_guideline 去改一份"看不见的"旧机制
            skipped.append({
                "key": key,
                "reason": "该键已下线（仅为旧工程兼容保留），请改用 write_project_guideline 写项目规范",
            })
            continue
        value = _parse_config_value(item.get("value"))
        if key == "plugin" or (key.startswith("plugin.") and key not in ("plugin.filePlugin", "plugin.textPlugins")):
            reason = validate_plugin_value(probe, key, value, plugin_catalog or {})
            if reason:
                skipped.append({"key": key, "reason": reason})
                continue
            _, module, setting = key.split(".", 2)
            current = probe.get("plugin", {}).get(module, {})
            before = current.get(setting, _MISSING)
            set_plugin_value(probe, key, value)
            applied.append({"key": key, "value": value})
            changes.append(_change(key, None if before is _MISSING else before, value,
                                   "add" if before is _MISSING else "replace"))
            continue
        before = _get_config_key(probe, key)
        if _set_config_key(probe, key, value):
            applied.append({"key": key, "value": value})
            kind = "add" if before is _MISSING else "replace"
            changes.append(_change(key, before if before is not _MISSING else None, value, kind))
        else:
            skipped.append({"key": key, "reason": "配置里不存在该键；只能修改已存在的键"})
    return applied, skipped, changes


def _tool_update_project_config(runner: AgentRunner, args: dict[str, Any]) -> Any:
    """修改项目配置：读-改-写回（与桌面端「项目配置」页同一通道）。

    普通键必须已存在；插件设置允许新增声明过的键，并验证类型与选项。"""
    updates = args.get("updates")
    if not isinstance(updates, list) or not updates:
        raise AgentToolError("updates must be a non-empty array of {key, value}")
    pid = runner._project_id()
    config_name = runner.state.config_file_name or DEFAULT_CONFIG_FILE
    data = runner._http_get(f"/api/projects/{pid}/config?config={urllib.parse.quote(config_name)}")
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgentToolError("项目配置读取失败")

    # 先在一份副本上算出「哪些键、改成什么」（与审批卡上的预览同一份判断），
    # 再把同一批改动打到真 config 上——同一个 _set_config_key、同样顺序。
    applied, skipped, changes = _plan_config_updates(config, updates, catalog_for_updates(runner, updates))
    for item in applied:
        if item["key"].startswith("plugin.") and item["key"] not in ("plugin.filePlugin", "plugin.textPlugins"):
            set_plugin_value(config, item["key"], item["value"])
        else:
            _set_config_key(config, item["key"], item["value"])

    if not applied:
        return {"updated": 0, "applied": [], "skipped": skipped or [{"key": "", "reason": "updates 为空"}]}

    runner._http_put(
        f"/api/projects/{pid}/config",
        {"config": config, "config_file_name": config_name},
    )
    result: dict[str, Any] = {"updated": len(applied), "applied": applied, "changes": changes}
    if skipped:
        result["skipped"] = skipped
    return result
