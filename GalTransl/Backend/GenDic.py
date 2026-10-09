import json, asyncio, os, re, bisect, time
import collections
from typing import List, Set, Dict, Optional, Tuple
from threading import Lock

from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.ConfigHelper import CProxyPool, initDictList
from GalTransl import LOGGER
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.CSentense import CSentense
from GalTransl.Dictionary import CGptDict, CNormalDic
from GalTransl.Utils import contains_katakana, is_all_chinese, decompress_file_lzma
from GalTransl.Backend.BaseTranslate import REASONING_FIELD_NAMES, BaseTranslate
from GalTransl.Backend.Prompts import (
    GENDIC_FOLLOWUP_PROMPT,
    GENDIC_NAME_PROMPT,
    GENDIC_PROMPT,
    GENDIC_REVIEW_FOLLOWUP_PROMPT,
    GENDIC_REVIEW_PROMPT,
    GENDIC_SYSTEM,
    H_WORDS_LIST,
)
from GalTransl.TerminalOutput import should_print_translation_logs, terminal_progress
from GalTransl.RuntimePaths import get_res_dir

# 正则补充层：连续片假名字串（含・）
_KATAKANA_SEQ_RE = re.compile(r"[ァ-ヶー・]{2,}")
# 多人同时说话的说话人标签（瑠那・萌美奈）用这些符号把几个名字连起来
_SPEAKER_JOINER_RE = re.compile(r"[・&＆+＋、,，/／]")
# 含句读、感叹号的是台词或句子片段
_SENTENCE_MARK_RE = re.compile(r"[。、，！？!?]")
# 送去提取的分片长度（字符）与最多提取的分片数：分片总数不超过上限时全文都读，
# 超过时用集合覆盖挑出覆盖候选词最多的那些分片
_SEGMENT_MAX_LEN = 1000
_MAX_SEGMENTS = 128
# 审校时每批的候选词条数；相关词条（名字与全名、昵称）尽量放同一批，一组超过这个数时允许放宽到 2 倍
_REVIEW_BATCH_SIZE = 40
# 说话人名按项目并发数动态分批；每批最多 60 个，目标至少 8 个，避免请求过碎
_NAME_BATCH_SIZE = 60
_NAME_MIN_BATCH_SIZE = 8
# 和别的词条都不沾边的这几类专有名词审校几乎不删，直接保留、不送审
_DIRECT_KEEP_CATEGORIES = ("地名", "组织", "种族")
# 被这么多个分片提取到、且译名一致（最多的译名占 2/3 以上）的词也直接保留（人名/称呼/其他/活动除外）
_DIRECT_KEEP_MIN_VOTES = 3
_REVIEW_DELETE_MARKS = ("DELETE", "删除")
# 多轮对话：同一个会话里连续提交多少段（提取的分片 / 审校的批次）。说明只发一次，模型记得前面
# 出现过的词，后面只需输出新词；每轮都会把之前的对话重发一遍，靠接口的前缀缓存才便宜
_SESSION_MAX_TURNS = 6
# 会话累计字数超过这个数就新开会话，避免撑爆后端上下文（按 1 字≈1 token 保守估计，留足余量）
_SESSION_MAX_CHARS = 24000
# 模型返回空响应时最多请求几次
_EMPTY_RESPONSE_ATTEMPTS = 2
# 模型常把书名号、引号一起抄进原词（『ユイ』），汇总前去掉，与不带括号的写法合并
_WRAPPING_BRACKETS = "『』「」【】《》〈〉"
# 审校没覆盖到（请求失败/中途停止）时，备注里有这些字样的词条直接保留
_KEEP_NOTE_KEYWORDS = ("人名", "姓", "昵称", "地名")
# 生成的字典按类别分区写入，类别由备注开头的类型决定；关键词按顺序匹配，都不中归「其他」
_DICT_CATEGORIES = (
    ("人名", ("人名", "姓", "全名", "名字", "角色", "主角")),
    ("称呼", ("称呼", "昵称", "外号", "绰号", "爱称", "自称", "尊称", "蔑称")),
    ("地名", ("地名", "国", "城", "市", "街", "大陆", "世界", "地区", "地点", "场所")),
    ("组织", ("组织", "社团", "学校", "学园", "机构", "委员", "店", "公司", "团", "部", "家族", "建筑", "设施")),
    ("种族", ("种族", "族")),
    ("技能", ("技能", "招式", "魔法", "法术", "能力")),
    ("物品", ("物品", "道具", "武器", "装备", "服装", "食", "菜", "甜点", "饮", "商品")),
    ("活动", ("活动", "比赛", "游戏", "节日", "仪式", "考试")),
    ("其他", ()),
)
# 分区标题行：----------↓人名↓----------（没有 Tab，GPT 字典加载时会跳过）
_SECTION_RE = re.compile(r"^-{3,}↓(.+?)↓-{3,}\s*$")


def _is_katakana_only(text: str) -> bool:
    """判断是否为纯片假名字串（含ー・），且长度>=2"""
    if len(text) < 2:
        return False
    for ch in text:
        cp = ord(ch)
        if ch in ("ー", "・"):
            continue
        if not (0x30A0 <= cp <= 0x30FF):
            return False
    return True


def _extract_regex_terms(text: str) -> Set[str]:
    """用正则补充提取专有名词候选：连续片假名字串。"""
    words: Set[str] = set()
    for m in _KATAKANA_SEQ_RE.finditer(text):
        w = m.group(0)
        if len(w) >= 2:
            words.add(w)
    return words


def _is_probable_character_name(name: str, tokenizer, name_set: Set[str]) -> bool:
    """说话人名是不是某个角色的名字。

    みんな/全員/子供たち/女の子Ａ/？？？ 这类泛称和临时代号不是，多人同时说话的标签
    （瑠那・萌美奈）也不是——以前把说话人名一律当人名塞进术语表，会产出「みんな→米娜」
    这种翻译时有害的词条。靠分词判断：含未登录词或固有名词的算名字；纯片假名的名字被切成
    好几段（サ|ヴィー|ネ）也算，常见的片假名词（メイド、ゴブリン）都是一个整词。
    说话人位置上的变量（$str20）一般是主角名字的占位符，也算，要让模型知道它指谁。
    """
    if not name:
        return False
    if re.search(r"[A-Za-z]", name):
        return True
    parts = [p for p in _SPEAKER_JOINER_RE.split(name) if p]
    if len(parts) >= 2 and any(p in name_set for p in parts):
        return False
    tokens = [t for t in tokenizer.tokenize(name) if t.surface().strip()]
    if len(name) == 1:
        # 单字名（玲）只认分词词典里的人名，母、姫这种普通词不算
        return bool(tokens) and (tokens[0].tag(0) or "").startswith("名詞-固有名詞")
    for token in tokens:
        tag = token.tag(0)
        if tag is None or tag.startswith("名詞-固有名詞"):
            return True
    return _is_katakana_only(name) and len(tokens) > 1


def _is_common_word(term: str, tokenizer) -> bool:
    """分词词典里收录的单个普通词（キノコ、スキル、ディーラー、通い妻、イク）：译者自己就会译，
    收进术语表反而会把一时的译法（通い妻→通勤妻）强加到全文。未登录词和固有名词不算。"""
    tokens = [t for t in tokenizer.tokenize(term) if t.surface().strip()]
    if len(tokens) != 1:
        return False
    tag = tokens[0].tag(0)
    return tag is not None and not tag.startswith("名詞-固有名詞")


def _category_of(note: str) -> str:
    """按备注开头的类型归类（「人名，女性」→人名，「社团/组织」→组织）。"""
    head = re.split(r"[，,、；;（(\s]", note.strip(), maxsplit=1)[0]
    for category, _ in _DICT_CATEGORIES:
        if head == category:
            return category
    for text in (head, note):
        for category, keywords in _DICT_CATEGORIES:
            if any(k in text for k in keywords):
                return category
    return "其他"


def _section_line(category: str) -> str:
    return f"----------↓{category}↓----------"


def _merge_into_sections(existing_lines: List[str], final_list: List[List[str]]) -> List[str]:
    """把新词条按类别并进字典文件：已有同名分区的接在该分区末尾，没有的在文件末尾新建分区。
    文件里原有的内容（包括用户手改的、没有分区的旧格式词条）原样保留。"""
    groups: Dict[str, List[str]] = collections.OrderedDict((c, []) for c, _ in _DICT_CATEGORIES)
    for src, dst, note in final_list:
        groups[_category_of(note)].append(f"{src}\t{dst}\t{note}")

    lines = list(existing_lines)
    if not any(line.strip() for line in lines):
        lines = ["# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki"]
    for category, items in groups.items():
        if not items:
            continue
        head = next(
            (i for i, line in enumerate(lines) if (m := _SECTION_RE.match(line)) and m.group(1) == category),
            None,
        )
        if head is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", _section_line(category)] + items
            continue
        end = head + 1
        while end < len(lines) and not _SECTION_RE.match(lines[end]):
            end += 1
        while end > head + 1 and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = items
    return lines


def _split_segments(lines: List[str], max_len: int) -> List[str]:
    """按行把文本切成约 max_len 字符的分片，不拆开单行。"""
    segments: List[str] = []
    buf: List[str] = []
    size = 0
    for line in lines:
        if size > max_len:
            segments.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        segments.append("\n".join(buf))
    return segments


def _example_lines(
    all_text: str, line_starts: List[int], speakers: List[str], term: str, count: int, width: int = 36
) -> List[str]:
    """取 term 第一次和居中那次出现的行做例句：只看一处常看不出这个词在作品里一般怎么用。"""
    positions = []
    pos = all_text.find(term)
    if pos >= 0:
        positions.append(pos)
        for _ in range(count // 2):
            pos = all_text.find(term, pos + len(term))
            if pos < 0:
                break
        if pos >= 0:
            positions.append(pos)
    examples: List[str] = []
    seen_lines: Set[int] = set()
    for pos in positions:
        idx = bisect.bisect_right(line_starts, pos) - 1
        if idx in seen_lines:
            continue
        seen_lines.add(idx)
        examples.append(_example_at(all_text, line_starts, speakers, term, pos, idx, width))
    return examples


def _example_at(
    all_text: str, line_starts: List[int], speakers: List[str], term: str, pos: int, idx: int, width: int
) -> str:
    """term 在 pos 处所在的行，行太长时只截 term 前后一段（保留说话人）。"""
    start = line_starts[idx]
    end = all_text.find("\n", pos)
    if end < 0:
        end = len(all_text)
    line = all_text[start:end]
    if len(line) <= width * 2 + len(term):
        return line.replace("\t", " ")
    rel = pos - start
    left = max(0, rel - width)
    right = min(len(line), rel + len(term) + width)
    snippet = ("…" if left > 0 else "") + line[left:right] + ("…" if right < len(line) else "")
    speaker = speakers[idx]
    if speaker and left > 0:
        snippet = f"{speaker}：{snippet}"
    return snippet.replace("\t", " ")


def _pack_review_batches(candidates: List[dict], batch_size: int) -> List[List[dict]]:
    """把候选词条分批送审：互为子串（ラエルダ / ラエルダ・レ・ファイルーダ）或共用・分隔的
    部分（…・ル・ドルード）、备注指向同一角色的昵称归为一组，同组放进同一批，审校时才能统一译法。"""
    n = len(candidates)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    srcs = [c["src"] for c in candidates]
    for i, a in enumerate(srcs):
        if len(a) < 2:
            continue
        for j, b in enumerate(srcs):
            if i != j and (a in b or a in candidates[j].get("note", "")):
                union(i, j)
    part_owner: Dict[str, int] = {}
    for i, s in enumerate(srcs):
        for part in s.split("・"):
            if len(part) >= 2 and part != s:
                if part in part_owner:
                    union(i, part_owner[part])
                else:
                    part_owner[part] = i

    groups: Dict[int, List[dict]] = collections.OrderedDict()
    for i in range(n):  # candidates 已按出现次数降序，组也就按组里最常见的词排序
        groups.setdefault(find(i), []).append(candidates[i])

    batches: List[List[dict]] = []
    current: List[dict] = []
    for group in groups.values():
        if len(group) > batch_size:
            if current:
                batches.append(current)
                current = []
            if len(group) <= batch_size * 2:
                batches.append(group)
            else:
                # 太大的组按原词排序后切开，前缀相同的（アリス / アリスティーネ）仍挨在一起
                ordered = sorted(group, key=lambda c: c["src"])
                for k in range(0, len(ordered), batch_size):
                    batches.append(ordered[k : k + batch_size])
            continue
        if len(current) + len(group) > batch_size:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)
    return batches


class _ChatSession:
    """一个多轮对话会话：累计的历史消息、轮数、字数，以及已经在这个会话里提示过的词。

    thinking 模式（DeepSeek 等）下，历史里每条 assistant 消息都要把当初的思考内容
    （reasoning_content / reasoning）原样带回去，少一条下一次请求就会被判 400
    「must be passed back to the API」。所以这里记下本会话命中的思考字段名，
    之后每条 assistant 消息都带上它（没给思考的轮次补空串），约定见 Agent/core.py。
    """

    def __init__(self):
        self.messages: List[dict] = []
        self.turns = 0
        self.chars = 0
        self.hinted: Set[str] = set()
        self.reasoning_field = ""

    def add_turn(self, prompt: str, reply: str, reasoning: Optional[dict] = None):
        text = str((reasoning or {}).get("text") or "")
        if text and not self.reasoning_field:
            self.reasoning_field = str((reasoning or {}).get("field") or REASONING_FIELD_NAMES[0])
        assistant: dict = {"role": "assistant", "content": reply}
        if self.reasoning_field:
            assistant[self.reasoning_field] = text
        self.messages += [{"role": "user", "content": prompt}, assistant]
        self.turns += 1
        # 思考内容也算进会话字数：它每一轮都要随历史重发，撑爆上下文的往往正是它
        self.chars += len(prompt) + len(reply) + len(text)

    def has_room(self, next_prompt_len: int) -> bool:
        return self.turns < _SESSION_MAX_TURNS and self.chars + next_prompt_len <= _SESSION_MAX_CHARS

    def reset(self):
        self.messages, self.turns, self.chars = [], 0, 0
        self.hinted = set()
        self.reasoning_field = ""


def _plan_sessions(items: List, workers: int) -> List[List]:
    """把任务按原顺序切成连续的几段，每段交给一个会话（连续的分片多半是同一场景）。
    每段的轮数取 floor(总数/并发数) 并封顶 _SESSION_MAX_TURNS：这样切出的会话数不少于并发数，
    小项目不会因为会话太少而并发度下降；分片数不足并发数时退化成每会话一轮。"""
    if not items:
        return []
    per_session = max(1, min(_SESSION_MAX_TURNS, len(items) // max(1, workers)))
    return [items[i : i + per_session] for i in range(0, len(items), per_session)]


def _plan_name_batches(names: List[str], workers: int) -> List[List[str]]:
    """名字不多时缩小批次以用上配置的并发；大项目仍封顶 60 个/批。"""
    workers = max(1, int(workers or 1))
    batch_size = min(_NAME_BATCH_SIZE, max(_NAME_MIN_BATCH_SIZE, (len(names) + workers - 1) // workers))
    return [names[i : i + batch_size] for i in range(0, len(names), batch_size)]


class GenDic(BaseTranslate):
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.dic_votes = collections.defaultdict(collections.Counter)
        self.note_votes = collections.defaultdict(collections.Counter)
        # 审校结果：src -> (dst, note)，None 表示审校删掉了这条
        self.review_decisions: Dict[str, Optional[Tuple[str, str]]] = {}
        # 角色名单独翻译的结果：src -> (dst, note)，None 表示判定不是角色名
        self.name_decisions: Dict[str, Optional[Tuple[str, str]]] = {}
        self.wokers = config.getKey("workersPerProject")
        self.counter_lock = Lock()
        self.progress_lock = Lock()
        self.progress_display_name = "GenDic 术语提取"
        self.progress_cache_key = "gendic_progress"
        self.progress_append_path = ""
        # 本轮进度：完成的项数（分片/批次）与开始时刻，用来算与进度同口径的速度（项/分）
        self.progress_done = 0
        self.progress_started_at = 0.0
        self.trans_prompt = ""
        self.init_chatbot(eng_type, config)
        backend_cfg = config.getBackendConfigSection("OpenAI-Compatible")
        raw_retry = backend_cfg.get("genDicMaxApiRetries", 6)
        try:
            parsed_retry = int(raw_retry)
        except (TypeError, ValueError):
            parsed_retry = 6
        self.gendic_max_api_retries = max(1, parsed_retry)
        pass

    def _load_existing_gpt_terms(self) -> Dict[str, Tuple[str, str]]:
        result_path = os.path.join(self.pj_config.getProjectDir(), "项目GPT字典-生成.txt")
        dict_cfg = self.pj_config.getDictCfgSection()
        gpt_dic_list = dict_cfg.get("gpt.dict", []) if dict_cfg else []
        default_dic_dir = dict_cfg.get("defaultDictFolder", "") if dict_cfg else ""
        dic_paths = initDictList(gpt_dic_list, default_dic_dir, self.pj_config.getProjectDir())

        existing_terms: Dict[str, Tuple[str, str]] = {}
        for dic_path in dic_paths:
            if os.path.abspath(dic_path) == os.path.abspath(result_path):
                continue
            dic_obj = CGptDict([dic_path])
            dic_list = getattr(dic_obj, "_dic_list", None) or []
            for dic in dic_list:
                if dic.search_word and dic.replace_word and dic.search_word not in existing_terms:
                    existing_terms[dic.search_word] = (dic.replace_word, getattr(dic, "note", "") or "")
        return existing_terms

    def _load_pre_dic(self) -> Optional[CNormalDic]:
        dict_cfg = self.pj_config.getDictCfgSection()
        if not dict_cfg:
            return None
        pre_dic_paths = initDictList(
            dict_cfg.get("preDict", []),
            dict_cfg.get("defaultDictFolder", ""),
            self.pj_config.getProjectDir(),
        )
        if not pre_dic_paths:
            return None
        pre_dic = CNormalDic(pre_dic_paths)
        if dict_cfg.get("sortDict", True):
            pre_dic.sort_dic()
        return pre_dic

    def _raise_if_stop_requested(self):
        if self._is_stop_requested(self.pj_config):
            from GalTransl.Service import JobCancelledError

            raise JobCancelledError()

    def _runtime_project_dir(self) -> str:
        return getattr(self.pj_config, "runtime_project_dir", self.pj_config.getProjectDir())

    def _update_runtime(self, **kwargs):
        try:
            from GalTransl.server import update_runtime_status

            update_runtime_status(self._runtime_project_dir(), **kwargs)
        except Exception:
            return

    def _record_runtime_error(
        self,
        *,
        kind: str,
        message: str,
        task_index: int | None = None,
        retry_count: int | None = None,
        model: str = "",
        level: str = "error",
    ):
        try:
            from GalTransl.server import record_runtime_error

            record_runtime_error(
                self._runtime_project_dir(),
                kind=kind,
                message=message,
                filename=self.progress_display_name,
                index_range=(str(task_index) if task_index is not None else ""),
                retry_count=retry_count,
                model=model,
                level=level,
            )
        except Exception:
            return

    def _load_existing_generated_terms(self, result_path: str) -> Dict[str, Tuple[str, str]]:
        terms: Dict[str, Tuple[str, str]] = {}
        if not os.path.exists(result_path):
            return terms
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    sp = line.split("\t")
                    if len(sp) < 2:  # 分区标题等非词条行
                        continue
                    src = sp[0].strip()
                    if src and src not in terms:
                        dst = sp[1].strip() if len(sp) > 1 else ""
                        note = sp[2].strip() if len(sp) > 2 else ""
                        terms[src] = (dst, note)
        except Exception:
            pass
        return terms

    def _build_text_lines(self, json_list: list) -> Tuple[List[str], List[str], Set[str]]:
        """把输入整理成送给模型的文本行（有说话人的写成「说话人：台词」），并收集说话人名。

        台词先过一遍译前字典：翻译时 GPT 字典是拿译前字典处理过的原文去匹配的，
        生成的词条也要按这份文本来写（比如注音 [う]兎[さき]咲 这种，译前字典会先处理掉）。
        """
        pre_dic = self._load_pre_dic()
        use_pre_dic_in_name = bool(self.pj_config.getDictCfgSection("usePreDictInName"))
        lines: List[str] = []
        speakers: List[str] = []
        name_set: Set[str] = set()
        for item in json_list:
            self._raise_if_stop_requested()
            message = item.get("message", "")
            if not isinstance(message, str):
                continue
            raw_name = item.get("name", item.get("names", ""))
            names = raw_name if isinstance(raw_name, list) else [raw_name]
            names = [n for n in names if isinstance(n, str) and n]
            speaker = "、".join(names)
            if pre_dic is not None:
                tran = CSentense(message, speaker)
                message = pre_dic.do_replace(message, tran)
                if speaker and use_pre_dic_in_name:
                    names = [pre_dic.do_replace(n, tran) for n in names]
                    speaker = "、".join(names)
            # 一句一行：句内换行换成空格，否则送审时的例句会把表格拆乱
            message = message.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            name_set.update(names)
            lines.append(f"{speaker}：{message}" if speaker else message)
            speakers.append(speaker)
        return lines, speakers, name_set

    def _save_generated_dictionary(self, final_list: List[List[str]], result_path: Optional[str] = None) -> str:
        path = result_path or os.path.join(self.pj_config.getProjectDir(), "项目GPT字典-生成.txt")
        existing_lines: List[str] = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing_lines = f.read().splitlines()
        if not final_list and existing_lines:
            return path
        lines = _merge_into_sections(existing_lines, final_list)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _prepare_runtime_progress(self, total_tasks: int):
        cache_dir = self.pj_config.getCachePath()
        os.makedirs(cache_dir, exist_ok=True)
        self.progress_append_path = os.path.join(
            cache_dir, f"{self.progress_cache_key}.append.jsonl"
        )
        try:
            if os.path.exists(self.progress_append_path):
                os.remove(self.progress_append_path)
        except Exception:
            pass

        self.progress_done = 0
        self.progress_started_at = time.monotonic()
        self._update_runtime(
            stage="GenDic 术语提取中",
            current_file="准备提取任务",
            workers_active=0,
            workers_configured=int(self.wokers or 1),
            file_totals={self.progress_display_name: int(total_tasks)},
            cache_file_display_map={self.progress_cache_key: self.progress_display_name},
        )

    def _progress_speed_lpm(self) -> float:
        """本轮到现在的平均速度，单位是「完成项/分」——与 x/130 项的进度同一个口径。

        runtime 默认的实时速度是「最近一分钟的成功事件数」：普通翻译一个成功事件正好是一句话，
        与进度（句）同单位；GenDic 的成功事件是抽出来的术语（一段就能抽出几十个），拿它算
        「预计剩余」会把 130 项分片的活算成还剩两分钟。所以这里自己报。
        """
        if self.progress_done <= 0 or self.progress_started_at <= 0:
            return 0.0
        elapsed = time.monotonic() - self.progress_started_at
        if elapsed <= 0:
            return 0.0
        return round(self.progress_done * 60 / elapsed, 1)

    def _append_runtime_progress(self, cache_key: str, success: bool, message: str = ""):
        if not self.progress_append_path:
            return
        entry = {
            "__cache_key": cache_key,
            "pre_dst": "OK" if success else "(Failed)",
            "problem": "" if success else (message or "GenDic 任务失败"),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self.progress_lock:
            with open(self.progress_append_path, "a", encoding="utf-8") as fp:
                fp.write(line)
                fp.write("\n")
            self.progress_done += 1
        # 每完成一项就把速度报上去：工作台的「实时速度 / 预计剩余」都按它算（见 server_runtime
        # 的 progress_speed_lpm），这一步与各阶段自己的 current_file 计数是两回事
        self._update_runtime(progress_speed_lpm=self._progress_speed_lpm())

    def _cleanup_runtime_progress(self):
        """收尾：删掉进度用的假缓存文件，并把速度清零。

        速度不清零的话，跑完（或被停止）之后工作台还会按最后那次平均速度算「预计剩余」——
        停在 38/130 的被停止任务会一直显示一个其实不会再动的倒计时。
        """
        if not self.progress_append_path:
            return
        try:
            if os.path.exists(self.progress_append_path):
                os.remove(self.progress_append_path)
        except Exception:
            pass
        finally:
            self.progress_append_path = ""
            self.progress_done = 0
            self.progress_started_at = 0.0
        self._update_runtime(progress_speed_lpm=0)

    def _record_runtime_success(self, index: int, source_preview: str, translation_preview: str):
        try:
            from GalTransl.server import record_runtime_success

            record_runtime_success(
                self._runtime_project_dir(),
                filename=self.progress_display_name,
                index=int(index),
                speaker=None,
                source_preview=source_preview,
                translation_preview=translation_preview,
                trans_by=self._get_chatbot_state()[1] or "GenDic",
            )
        except Exception:
            return

    async def _ask_gendic(
        self, prompt: str, task_label: str, task_index: int, session: Optional["_ChatSession"] = None
    ) -> Optional[str]:
        """调一次模型，失败/空响应时记错误并返回 None。

        传了 session 时接着这个会话的历史发（多轮对话），成功后把这一轮追加进去。
        thinking 模式的思考内容也一并记进会话：历史里少一条 reasoning_content，
        下一次请求会被 provider 判 400。流式接口偶尔整段返回空（不抛异常，ask_chatbot
        不会重试），空响应时再请求一次，否则这一片里的词就整片丢了。"""
        messages = [{"role": "system", "content": GENDIC_SYSTEM}]
        if session is not None:
            messages += session.messages
        messages.append({"role": "user", "content": prompt})
        for _ in range(_EMPTY_RESPONSE_ATTEMPTS):
            self._raise_if_stop_requested()
            reasoning: dict = {}
            try:
                rsp, token = await self.ask_chatbot(
                    messages=messages,
                    file_name=self.progress_display_name,
                    max_retry_count=self.gendic_max_api_retries,
                    reasoning_holder=reasoning,
                    progress_file=self.progress_display_name,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                from GalTransl.Service import JobCancelledError

                if isinstance(e, JobCancelledError):
                    raise
                error_message = (
                    f"GenDic {task_label} {task_index} LLM请求失败，已重试{self.gendic_max_api_retries}次，放弃: {e}"
                )
                LOGGER.error(error_message)
                self._record_runtime_error(
                    kind="api",
                    message=error_message,
                    task_index=task_index,
                    retry_count=self.gendic_max_api_retries,
                    model=self._get_chatbot_state()[1] or "",
                )
                return None

            if should_print_translation_logs(self.pj_config):
                print(rsp)

            if isinstance(rsp, str) and rsp.strip() != "":
                # 有的接口把思考过程放在正文里
                content = rsp.split("</think>")[-1]
                if session is not None:
                    # 思考内容跟着这一轮的 assistant 消息存进会话，后面每轮原样回传
                    session.add_turn(prompt, content, reasoning)
                return content

        warning_message = f"GenDic {task_label} {task_index} 返回空响应，放弃"
        LOGGER.warning(warning_message)
        self._record_runtime_error(
            kind="parse",
            message=warning_message,
            task_index=task_index,
            level="warning",
        )
        return None

    async def _translate_names_batch(
        self,
        names: List[str],
        batch_index: int,
        speaker_counts: Dict[str, int],
        all_text: str,
        line_starts: List[int],
        speakers: List[str],
        known_map: Dict[str, Tuple[str, str]],
    ) -> bool:
        """把一批说话人名交给模型统一翻译。name 字段每一项最终都要显示成中文，
        所以泛称、临时代号也翻（みんな→大家），变量（$str20）照抄并注明代表谁。"""
        rows = []
        for name in names:
            examples = _example_lines(all_text, line_starts, speakers, f"{name}：", speaker_counts.get(name, 1))
            rows.append("\t".join([name, str(speaker_counts.get(name, 0)), "／".join(examples)]))
        fixed_rows = [
            f"{src}\t{dst}\t{note}".rstrip("\t")
            for src, (dst, note) in known_map.items()
            if any((len(src) >= 2 and src in n) or (len(n) >= 2 and n in src) for n in names)
        ]
        prompt = GENDIC_NAME_PROMPT.format(
            fixed="\n".join(fixed_rows[:60]) if fixed_rows else "无", input="\n".join(rows)
        )
        rsp = await self._ask_gendic(prompt, "人名批次", batch_index)
        if rsp is None:
            return False

        wanted = set(names)
        decided = 0
        for line in rsp.split("\n"):
            self._raise_if_stop_requested()
            sp = line.split("\t")
            if len(sp) < 2:
                continue
            src = sp[0].strip()
            dst = sp[1].strip()
            # 模型仍写了 DELETE 的不算结果，这个名字交回提取和兜底规则处理
            if src not in wanted or not dst or any(dst.upper().startswith(m) for m in _REVIEW_DELETE_MARKS):
                continue
            note = sp[2].strip() if len(sp) > 2 else ""
            self.name_decisions[src] = (dst, note if len(note) <= 30 else "")
            decided += 1
            if decided <= 3:
                self._record_runtime_success(index=batch_index, source_preview=src, translation_preview=f"{dst}｜{note}")

        if decided == 0:
            warning_message = f"GenDic 人名批次 {batch_index} 未解析到有效结果，这些名字按原流程处理"
            LOGGER.warning(warning_message)
            self._record_runtime_error(kind="parse", message=warning_message, task_index=batch_index, level="warning")
            return False
        return True

    async def llm_gen_dic(
        self, text: str, name_list=[], task_index: int = 0, session: Optional[_ChatSession] = None
    ) -> bool:
        """提取一个分片。传了 session 时作为多轮对话的一轮发送：会话里已经提示过的已有词条和人名
        不再重复提示；装不下（轮数/字数到上限）就先清空会话；这一轮失败时用全新会话重试一次。"""
        self._raise_if_stop_requested()
        hinted = session.hinted if session is not None else set()
        hint = "无"
        name_hit = [name for name in name_list if name in text and name not in hinted]

        parts: List[str] = []
        existing_dict_map = getattr(self, "existing_dict_map", None) or {}
        appeared: Dict[str, Tuple[str, str]] = {}
        if existing_dict_map:
            appeared = {
                k: v for k, v in existing_dict_map.items()
                if k in text and k not in hinted
            }
            if appeared:
                lines = [f"{src}\t{dst}\t{note}" for src, (dst, note) in appeared.items()]
                parts.append("以下词汇已有确定翻译，请严格保持一致，不要重复提取：\n" + "\n".join(lines))
        if name_hit:
            decided = [n for n in name_hit if self.name_decisions.get(n)]
            undecided = [n for n in name_hit if not self.name_decisions.get(n)]
            if decided:
                # 说话人名已经统一译过：给出译名供全名、昵称对齐，但不要每片再输出一遍
                lines = [f"{n}\t{self.name_decisions[n][0]}" for n in decided]
                parts.append("以下角色名已收录（译名供参考，相关的全名、昵称请与之一致），不要再输出：\n" + "\n".join(lines))
            if undecided:
                parts.append("以下说话人名是角色名，要加入术语表：\n" + "\n".join(undecided))
        if parts:
            hint = "\n\n".join(parts)

        rsp = None
        if session is not None and session.turns > 0:
            prompt = GENDIC_FOLLOWUP_PROMPT.format(input=text, hint=hint)
            if session.has_room(len(prompt)):
                rsp = await self._ask_gendic(prompt, "分片", task_index, session)
            if rsp is not None:
                hinted.update(appeared)
                hinted.update(name_hit)
            else:
                # 装不下或这一轮失败（比如超出上下文）：换全新会话，按第一轮重发（提示也要重新给全）
                session.reset()
                return await self.llm_gen_dic(text, name_list, task_index, session)
        else:
            prompt = GENDIC_PROMPT.format(input=text, hint=hint)
            rsp = await self._ask_gendic(prompt, "分片", task_index, session)
            if rsp is not None and session is not None:
                hinted.update(appeared)
                hinted.update(name_hit)
        if rsp is None:
            return False

        valid_entries = []
        got_null = False
        for line in rsp.split("\n"):
            self._raise_if_stop_requested()
            sp = line.split("\t")
            if len(sp) < 2:
                continue
            if "日文" in sp[0]:
                continue
            src = sp[0].strip().strip(_WRAPPING_BRACKETS)
            dst = sp[1].strip().strip(_WRAPPING_BRACKETS)
            note = sp[2].strip() if len(sp) > 2 else ""
            if len(note) > 30:
                note = ""
            if src == "NULL":
                got_null = True
                continue
            if not src or not dst:
                continue
            valid_entries.append((src, dst, note))

        if not valid_entries:
            if got_null:
                return True
            warning_message = f"GenDic 分片 {task_index} 未解析到有效词条，放弃该分片"
            LOGGER.warning(warning_message)
            self._record_runtime_error(
                kind="parse",
                message=warning_message,
                task_index=task_index,
                level="warning",
            )
            return False

        for idx, (src, dst, note) in enumerate(valid_entries):
            if idx < 3:
                self._record_runtime_success(
                    index=task_index,
                    source_preview=src,
                    translation_preview=f"{dst}｜{note}",
                )
            with self.counter_lock:
                self.dic_votes[src][dst] += 1
                if note:
                    self.note_votes[src][note] += 1
        return True

    def _collect_candidates(
        self,
        all_text: str,
        known_terms: Set[str],
        name_set: Set[str] = frozenset(),
        character_names: Set[str] = frozenset(),
        tokenizer=None,
    ) -> Tuple[List[dict], int]:
        """汇总各分片的提取结果，得到送审的候选词条（按全文出现次数降序）。"""
        candidates: List[dict] = []
        duplicates = 0
        with self.counter_lock:
            votes_snapshot = {src: collections.Counter(v) for src, v in self.dic_votes.items()}
            notes_snapshot = {src: collections.Counter(v) for src, v in self.note_votes.items()}
        for src, votes in votes_snapshot.items():
            if self.name_decisions.get(src):  # 说话人名已单独译过
                continue
            if src in known_terms:
                duplicates += 1
                continue
            if "NULL" in src or src in H_WORDS_LIST:
                continue
            # 单字会在翻译时到处误匹配（说话人名除外，是不是角色名交给审校）；带～的是拉长音的
            # 临时写法（ダ～リ～ン）；带句读、感叹号的是台词不是词
            if len(src) < 2 and src not in name_set:
                continue
            if "～" in src or "〜" in src or _SENTENCE_MARK_RE.search(src):
                continue
            # 模型改写过、编造的词在原文里找不到，翻译时也匹配不上
            count = all_text.count(src)
            if count == 0:
                continue
            dsts = [dst for dst, _ in votes.most_common()]
            note_counter = notes_snapshot.get(src)
            note = note_counter.most_common(1)[0][0] if note_counter else ""
            if "拟声" in note:
                continue
            if "（" not in src and "（" in dsts[0]:
                continue
            if (
                tokenizer is not None
                and src not in character_names
                and not any(k in note for k in _KEEP_NOTE_KEYWORDS)
                and _is_common_word(src, tokenizer)
            ):
                continue
            candidates.append(
                {"src": src, "dsts": dsts, "note": note, "count": count, "votes": sum(votes.values())}
            )
        candidates.sort(key=lambda c: (-c["count"], -c["votes"]))
        return candidates, duplicates

    async def _review_batch(
        self,
        batch: List[dict],
        batch_index: int,
        all_text: str,
        line_starts: List[int],
        speakers: List[str],
        known_map: Dict[str, Tuple[str, str]],
        session: Optional[_ChatSession] = None,
        *,
        catalog: str = "",
        protected_names: Set[str] = frozenset(),
    ) -> bool:
        """一次完成术语筛选与译名核对；各批读同一份全表和固定译名，只提交本批结果。"""
        rows = []
        for c in batch:
            examples = _example_lines(all_text, line_starts, speakers, c["src"], c["count"])
            rows.append(
                "\t".join([c["src"], str(c["count"]), "｜".join(c["dsts"][:3]), c["note"], "／".join(examples)])
            )

        hinted = session.hinted if session is not None else set()
        batch_srcs = {c["src"] for c in batch}
        fixed_rows = []
        fixed_srcs = []
        for src, (dst, note) in known_map.items():
            if src in hinted:
                continue
            # 爱称与主名可能互不包含（トレニャン / トレニア），参考表不能按子串截断。
            fixed_rows.append(f"{src}\t{dst}\t{note}".rstrip("\t"))
            fixed_srcs.append(src)
        prompt_args = dict(
            fixed="\n".join(fixed_rows) or "无",
            catalog=catalog or "无",
            protected="、".join(sorted(batch_srcs & protected_names)) or "无",
            input="\n".join(rows),
        )

        rsp = None
        if session is not None and session.turns > 0:
            prompt = GENDIC_REVIEW_FOLLOWUP_PROMPT.format(**prompt_args)
            if session.has_room(len(prompt)):
                rsp = await self._ask_gendic(prompt, "审校批次", batch_index, session)
            if rsp is None:
                session.reset()
                return await self._review_batch(
                    batch, batch_index, all_text, line_starts, speakers, known_map, session,
                    catalog=catalog, protected_names=protected_names,
                )
        else:
            prompt = GENDIC_REVIEW_PROMPT.format(**prompt_args)
            rsp = await self._ask_gendic(prompt, "审校批次", batch_index, session)
        if rsp is None:
            return False
        hinted.update(fixed_srcs)

        decisions: Dict[str, Optional[Tuple[str, str]]] = {}
        invalid = False
        original_notes = {c["src"]: c["note"] for c in batch}
        previews = []
        for line in rsp.split("\n"):
            self._raise_if_stop_requested()
            sp = line.split("\t")
            if len(sp) < 2:
                continue
            src = sp[0].strip()
            dst = sp[1].strip()
            if src not in batch_srcs:
                continue
            if src in decisions or not dst or dst.upper() == "NULL":
                invalid = True
                continue
            note = sp[2].strip() if len(sp) > 2 else ""
            if any(dst.upper().startswith(mark) for mark in _REVIEW_DELETE_MARKS):
                if src in protected_names:
                    invalid = True
                    continue
                decisions[src] = None
            else:
                # 不接受另改主名的整批结果：其中的复合行也可能已跟着错误的新主名改写。
                if src in known_map and dst != known_map[src][0]:
                    invalid = True
                    continue
                decisions[src] = (dst, note if note and len(note) <= 30 else original_notes[src])
                if len(previews) < 3:
                    previews.append((src, dst, note))

        if invalid or set(decisions) != batch_srcs:
            warning_message = f"GenDic 审校批次 {batch_index} 未完整返回有效结果，该批保留原结果或按规则筛选，需复核"
            LOGGER.warning(warning_message)
            self._record_runtime_error(
                kind="parse",
                message=warning_message,
                task_index=batch_index,
                level="warning",
            )
            return False
        self.review_decisions.update(decisions)
        for src, dst, note in previews:
            self._record_runtime_success(
                index=batch_index,
                source_preview=src,
                translation_preview=f"{dst}｜{note}",
            )
        return True

    def _triage_candidates(
        self, candidates: List[dict], name_srcs: Set[str]
    ) -> Tuple[List[dict], List[dict]]:
        """分流：和别的候选、角色名都不沾边（互不包含、不共用・分段）的词条，属于地名/组织/种族，
        或多片提取到且译名一致的，直接保留；人名/称呼始终送审，保证昵称不漏查。地名/组织/种族几乎不会被删，
        要删和要统一译法的集中在「其他」「活动」「称呼」和带名字的词条上。"""
        related: Set[str] = set()
        srcs = [c["src"] for c in candidates] + sorted(name_srcs)
        part_owner: Dict[str, str] = {}
        for i, a in enumerate(srcs):
            for b in srcs[i + 1 :]:
                if (len(a) >= 2 and a in b) or (len(b) >= 2 and b in a):
                    related.update((a, b))
            for part in a.split("・"):
                if len(part) >= 2 and part != a:
                    if part in part_owner:
                        related.update((a, part_owner[part]))
                    else:
                        part_owner[part] = a

        direct, to_review = [], []
        for c in candidates:
            category = _category_of(c["note"])
            top_votes = self.dic_votes[c["src"]][c["dsts"][0]] if c["src"] in self.dic_votes else 0
            consistent = c["votes"] >= _DIRECT_KEEP_MIN_VOTES and top_votes * 3 >= c["votes"] * 2
            if category not in ("人名", "称呼") and c["src"] not in related and (
                category in _DIRECT_KEEP_CATEGORIES or (consistent and category not in ("其他", "活动"))
            ):
                direct.append(c)
            else:
                to_review.append(c)
        return direct, to_review

    def _build_final_list(
        self,
        candidates: List[dict],
        name_set: Set[str],
        character_names: Set[str],
        all_text: str = "",
    ) -> List[List[str]]:
        final_list: List[List[str]] = []
        # 单独译过的说话人名（含泛称），按全文出现次数排在前面
        names = [(src, d) for src, d in self.name_decisions.items() if d]
        names.sort(key=lambda item: -all_text.count(item[0]))
        for src, (dst, note) in names:
            dst, note = self.review_decisions.get(src) or (dst, note)
            final_list.append([src, dst, note or "人名"])
        for c in candidates:
            src = c["src"]
            if src in self.review_decisions:
                decision = self.review_decisions[src]
                if decision is not None:
                    dst, note = decision
                    final_list.append([src, dst, note or c["note"]])
                continue
            # 没审校到（请求失败、漏答或中途停止）：按规则兜底
            if src in name_set and src not in character_names:
                continue
            if (
                c["votes"] >= 2
                or c["count"] >= 2
                or any(k in c["note"] for k in _KEEP_NOTE_KEYWORDS)
            ):
                final_list.append([src, c["dsts"][0], c["note"]])
        return final_list

    async def _run_final_review(
        self,
        candidates: List[dict],
        name_set: Set[str],
        known_map: Dict[str, Tuple[str, str]],
        all_text: str,
        line_starts: List[int],
        speakers: List[str],
    ):
        """合并术语筛选与译名校对，固定参考后按批调度并发；每条最多送审一次。"""
        from GalTransl.Service import JobCancelledError

        named = {src: d for src, d in self.name_decisions.items() if d}
        direct, to_review = self._triage_candidates(candidates, set(named))
        for c in direct:
            self.review_decisions[c["src"]] = (c["dsts"][0], c["note"])
        name_candidates = [
            {"src": src, "dsts": [dst], "note": note or "人名", "count": all_text.count(src)}
            for src, (dst, note) in named.items()
        ]
        to_review = to_review + name_candidates
        to_review.sort(key=lambda c: -c["count"])
        batches = _pack_review_batches(to_review, _REVIEW_BATCH_SIZE)
        if not batches:
            return

        # 主名沿用预译/提取已选的写法；复合名、全名与昵称仍是待校对项。
        # 所有批次拿相同快照，不依赖别的批次何时完成，也不逐批重译主名。
        review_ref = {
            c["src"]: (c["dsts"][0], c["note"])
            for c in to_review
            if _category_of(c["note"]) == "人名"
            and not _SPEAKER_JOINER_RE.search(c["src"])
            and not _SENTENCE_MARK_RE.search(c["src"])
            and not any(k in c["note"] for k in ("昵称", "爱称", "外号", "绰号", "全名", "简称", "缩写"))
        }
        review_ref.update({c["src"]: (c["dsts"][0], c["note"]) for c in direct})
        review_ref.update(known_map)
        catalog = "\n".join(
            "\t".join([c["src"], "｜".join(c["dsts"][:3]), c["note"]])
            for c in name_candidates + candidates
        )
        protected_names = set(named) | name_set
        workers = max(1, int(self.wokers or 1))
        sem = asyncio.Semaphore(workers)
        LOGGER.info(
            f"术语提取完成，{len(direct)}条直接保留，{len(to_review)}条分{len(batches)}批联合终审"
            f"（术语筛选与译名一致性一次完成，并发{min(workers, len(batches))}）"
        )
        self._update_runtime(
            stage="GenDic 联合终审中",
            current_file=f"终审 0/{len(batches)}",
            workers_active=min(workers, len(batches)),
            file_totals={self.progress_display_name: self.progress_done + len(batches)},
        )

        async def review_one(index, batch):
            async with sem:
                self._raise_if_stop_requested()
                try:
                    ok = await self._review_batch(
                        batch, index, all_text, line_starts, speakers, review_ref,
                        catalog=catalog, protected_names=protected_names,
                    )
                    return index, ok, "" if ok else "联合终审未完成"
                except (asyncio.CancelledError, JobCancelledError):
                    raise
                except Exception as exc:
                    LOGGER.error(f"联合终审时出错: {exc}")
                    return index, False, str(exc)

        tasks = [asyncio.create_task(review_one(i, batch)) for i, batch in enumerate(batches)]
        with terminal_progress(
            should_print_translation_logs(self.pj_config), title="联合终审中……", total=len(batches)
        ) as bar:
            self.pj_config.bar = bar
            completed = 0
            try:
                for future in asyncio.as_completed(tasks):
                    index, ok, error_message = await future
                    self._raise_if_stop_requested()
                    completed += 1
                    self._append_runtime_progress(f"gendic-review-{index}", ok, error_message)
                    self._update_runtime(
                        current_file=f"终审 {completed}/{len(batches)}",
                        workers_active=min(workers, len(batches) - completed),
                    )
                    bar()
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    async def batch_translate(
        self,
        json_list: list,
    ) -> bool:
        from GalTransl.Service import JobCancelledError

        name_set: Set[str] = set()
        character_names: Set[str] = set()
        tokenizer = None
        all_text = ""
        known_map: Dict[str, Tuple[str, str]] = {}
        candidates: Optional[List[dict]] = None
        duplicates = 0
        cancelled_error: Optional[JobCancelledError] = None

        result_path = os.path.join(self.pj_config.getProjectDir(), "项目GPT字典-生成.txt")
        existing_file_terms = self._load_existing_generated_terms(result_path)

        try:
            self._raise_if_stop_requested()
            self._update_runtime(stage="GenDic 分词处理中", current_file="准备分词")
            with terminal_progress(should_print_translation_logs(self.pj_config), title="载入分词……") as bar:
                # get tmp dir
                import tempfile

                tmp_dir = tempfile.gettempdir()
                model_path = os.path.join(tmp_dir, "bccwj-suw+unidic_pos+pron.model")
                if not os.path.exists(model_path):
                    zst_path = str(get_res_dir() / "bccwj-suw+unidic_pos+pron.model.xz")
                    decompress_file_lzma(zst_path, model_path)
                bar()
                import vaporetto

                try:
                    with open(model_path, "rb") as fp:
                        model = fp.read()
                    tokenizer = vaporetto.Vaporetto(model, predict_tags=True)
                except Exception as e:
                    LOGGER.error(e)
                    LOGGER.error("载入分词模型失败，请尝试重启程序")
                    os.remove(model_path)
                    return False
                bar()

                lines, speakers, name_set = self._build_text_lines(json_list)
                character_names = {
                    name for name in name_set if _is_probable_character_name(name, tokenizer, name_set)
                }
                all_text = "\n".join(lines)
                line_starts: List[int] = []
                offset = 0
                for line in lines:
                    line_starts.append(offset)
                    offset += len(line) + 1
                segment_list = _split_segments(lines, _SEGMENT_MAX_LEN)
                bar.title = "处理分词……"

                # 收集已有 GPT 字典翻译（排除当前生成文件），用于提示与最终结果去重
                existing_dict_map = self._load_existing_gpt_terms()
                self.existing_dict_map = existing_dict_map
                known_map = dict(existing_file_terms)
                known_map.update(existing_dict_map)

                word_counter = collections.Counter()
                segment_words_list = []
                for item in segment_list:
                    self._raise_if_stop_requested()
                    tmp_words = set()
                    for token in tokenizer.tokenize(item):
                        surf = token.surface()
                        tag = token.tag(0)
                        if len(surf) <= 1:
                            continue
                        # 未登录词（多半是作品自造的名字）与固有名词
                        if tag is None:
                            if not (contains_katakana(surf) or is_all_chinese(surf)):
                                continue
                        elif not tag.startswith("名詞-固有名詞"):
                            continue
                        tmp_words.add(surf)
                        word_counter[surf] += 1

                    # 正则补充层：片假名序列
                    for w in _extract_regex_terms(item):
                        tmp_words.add(w)
                        word_counter[w] += 1

                    # 名字强制保留到 Set Cover（确保仅出现一次的名字也被覆盖）
                    for name in character_names:
                        if name in item and len(name) >= 2:
                            tmp_words.add(name)
                            word_counter[name] += 1

                    segment_words_list.append(tmp_words)
                    bar()

            if len(segment_list) <= _MAX_SEGMENTS:
                index_list = list(range(len(segment_list)))
            else:
                # 放宽过滤：名字和纯片假名词允许出现1次，其他仍需>=2
                word_counter = {
                    word: count for word, count in word_counter.items()
                    if count >= 2 or word in character_names or _is_katakana_only(word)
                }
                segment_words_list_new = []
                for item in segment_words_list:
                    self._raise_if_stop_requested()
                    segment_words_list_new.append({word for word in item if word in word_counter})
                index_list = sorted(
                    solve_sentence_selection(
                        segment_words_list_new, max_select=_MAX_SEGMENTS, name_set=character_names
                    )
                )
            # 说话人名先统一译一遍（name 字段每项都要译，泛称也是），提取时就只需找其他词
            speaker_counts = collections.Counter(n for s in speakers if s for n in s.split("、"))
            pending_names = sorted(
                (n for n in name_set if n not in known_map), key=lambda n: (-speaker_counts.get(n, 0), n)
            )
            workers = max(1, int(self.wokers or 1))
            name_batches = _plan_name_batches(pending_names, workers)
            prep_tasks = len(name_batches)
            self._prepare_runtime_progress(len(index_list) + prep_tasks)
            sem = asyncio.Semaphore(workers)
            if name_batches:
                LOGGER.info(
                    f"先翻译{len(pending_names)}个说话人名，分{len(name_batches)}批"
                    f"（每批最多{max(map(len, name_batches))}个，并发{min(workers, len(name_batches))}）"
                )
                self._update_runtime(
                    stage="GenDic 人名翻译中", current_file=f"人名 0/{len(name_batches)}",
                    workers_active=min(workers, len(name_batches)),
                )

                async def name_item_async(batch_index, names):
                    async with sem:
                        try:
                            ok = await self._translate_names_batch(
                                names, batch_index, speaker_counts, all_text, line_starts, speakers, known_map
                            )
                            return batch_index, bool(ok), ""
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            if isinstance(e, JobCancelledError):
                                raise
                            LOGGER.error(f"翻译人名时出错: {e}")
                            return batch_index, False, str(e)

                name_tasks = [asyncio.create_task(name_item_async(i, b)) for i, b in enumerate(name_batches)]
                try:
                    completed_names = 0
                    for f in asyncio.as_completed(name_tasks):
                        batch_index, ok, error_message = await f
                        self._raise_if_stop_requested()
                        self._append_runtime_progress(f"gendic-name-{batch_index}", ok, error_message)
                        completed_names += 1
                        self._update_runtime(
                            stage="GenDic 人名翻译中",
                            current_file=f"人名 {completed_names}/{len(name_tasks)}",
                            workers_active=min(int(self.wokers or 1), len(name_tasks) - completed_names),
                        )
                except BaseException:
                    for task in name_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*name_tasks, return_exceptions=True)
                    raise

            character_name_list = sorted(character_names)
            # 多轮对话：连续的若干分片（多半是同一场景）交给同一个会话，说明只发一次，
            # 之后每轮只发新片段并要求只输出新词，减少重复思考与重复输出
            session_plans = _plan_sessions(index_list, int(self.wokers or 1))
            LOGGER.info(
                f"启动{self.wokers}个工作线程，{len(index_list)}个分片分{len(session_plans)}个会话提取"
                f"（每会话最多{_SESSION_MAX_TURNS}轮）"
            )
            completed_tasks = 0

            async def process_session_async(session_items):
                """一个会话按顺序处理连续的几个分片，返回每一片的处理结果供进度上报。"""
                async with sem:
                    session = _ChatSession()
                    results = []
                    for idx in session_items:
                        self._raise_if_stop_requested()
                        try:
                            ok = await self.llm_gen_dic(
                                segment_list[idx],
                                name_list=character_name_list,
                                task_index=idx,
                                session=session,
                            )
                            results.append((idx, bool(ok), ""))
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            from GalTransl.Service import JobCancelledError

                            if isinstance(e, JobCancelledError):
                                raise
                            LOGGER.error(f"处理任务时出错: {e}")
                            results.append((idx, False, str(e)))
                    return results

            tasks = [asyncio.create_task(process_session_async(items)) for items in session_plans]

            with terminal_progress(
                should_print_translation_logs(self.pj_config),
                title="生成中……",
                total=len(index_list),
            ) as bar:
                self.pj_config.bar = bar
                self._update_runtime(
                    stage="GenDic 术语提取中",
                    current_file=f"提取 0/{len(index_list)}",
                    workers_active=int(self.wokers or 1),
                )
                try:
                    for f in asyncio.as_completed(tasks):
                        for idx, ok, error_message in await f:
                            self._raise_if_stop_requested()
                            completed_tasks += 1
                            self._append_runtime_progress(f"gendic-task-{int(idx)}", ok, error_message)
                            remaining = len(index_list) - completed_tasks
                            self._update_runtime(
                                stage="GenDic 术语提取中",
                                current_file=f"提取 {completed_tasks}/{len(index_list)}",
                                workers_active=min(int(self.wokers or 1), remaining),
                            )
                            bar()
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

            # 终审只跑一轮：说话人、昵称、术语一起检查，独立批次并发。
            candidates, duplicates = self._collect_candidates(
                all_text, set(known_map), name_set, character_names, tokenizer
            )
            await self._run_final_review(
                candidates, name_set, known_map, all_text, line_starts, speakers
            )

        except JobCancelledError as ex:
            cancelled_error = ex
            self._update_runtime(stage="GenDic 停止处理中", current_file="整理当前结果", workers_active=0)
        finally:
            self._cleanup_runtime_progress()

        if cancelled_error is None:
            self._update_runtime(stage="GenDic 生成字典中", current_file="汇总结果并写入字典", workers_active=0)
        if candidates is None:
            candidates, duplicates = self._collect_candidates(
                all_text, set(known_map), name_set, character_names, tokenizer
            )
        final_list = self._build_final_list(candidates, name_set, character_names, all_text)
        result_path = self._save_generated_dictionary(final_list, result_path)
        added_count = len(final_list)
        setattr(self.pj_config, "gendic_added_count", added_count)
        setattr(self.pj_config, "gendic_duplicated_count", duplicates)

        if cancelled_error is not None:
            setattr(self.pj_config, "gendic_partial_saved", True)
            LOGGER.info(f"GenDic 已停止，使用当前结果生成字典，新增{added_count}条，重复{duplicates}条，保存到{result_path}")
            self._update_runtime(stage="", current_file="", workers_active=0)
            raise cancelled_error

        LOGGER.info(f"字典生成完成，新增{added_count}条，重复{duplicates}条，保存到{result_path}")
        self._update_runtime(stage="", current_file="", workers_active=0)
        return True


def solve_sentence_selection(sentences, max_select=128, name_set=None):
    """
    加权贪心集合覆盖 + 逆向精简。

    策略：
    1. 词权重 = 1 / doc_freq，越稀有的词权重越高；
    2. name_set 中的词额外乘高系数，确保名字相关切片优先入选；
    3. 贪心阶段每次选带来最大加权新覆盖的句子；
    4. 若选出的句子超过 max_select，逆向精简：
       计算每个句子的边际贡献（该句独有的词加权总和），
       若移除会导致名字词完全丢失，则大幅抬高边际贡献避免被剔除，
       循环剔除边际贡献最小的句子直到 <= max_select。
    """
    if not sentences:
        return []

    name_set = name_set or set()

    # 1) 词频
    doc_freq = collections.Counter()
    for s in sentences:
        for w in s:
            doc_freq[w] += 1

    # 2) 词权重函数
    def _weight(word):
        w = 1.0 / doc_freq[word]
        if word in name_set:
            w *= 5.0
        return w

    # 3) 加权贪心选择
    covered = set()
    selected = []
    remaining = set(range(len(sentences)))

    while remaining and len(selected) < max_select:
        best_idx = -1
        best_score = -1.0

        for idx in remaining:
            s = sentences[idx]
            new_words = s - covered
            if not new_words:
                continue
            score = sum(_weight(w) for w in new_words)
            # 平局打破：新覆盖相同则优先选总长度更短/更精炼的句子
            if score > best_score or (
                abs(score - best_score) < 1e-9 and len(s) < len(sentences[best_idx])
            ):
                best_score = score
                best_idx = idx

        if best_idx == -1:
            break  # 没有新覆盖可带来

        selected.append(best_idx)
        covered.update(sentences[best_idx])
        remaining.discard(best_idx)

    # 4) 逆向精简：若超过 max_select，剔除冗余
    if len(selected) > max_select:
        cover_count = collections.Counter()
        for idx in selected:
            for w in sentences[idx]:
                cover_count[w] += 1

        while len(selected) > max_select:
            min_idx = -1
            min_contrib = float("inf")

            for i, idx in enumerate(selected):
                contrib = 0.0
                would_lose_name = False
                for w in sentences[idx]:
                    if cover_count[w] == 1:
                        contrib += _weight(w)
                        if w in name_set:
                            would_lose_name = True
                # 若移除会导致名字词丢失，大幅抬高边际贡献使其不被剔除
                if would_lose_name:
                    contrib += 1e6
                if contrib < min_contrib:
                    min_contrib = contrib
                    min_idx = i

            if min_idx == -1:
                break

            removed = selected.pop(min_idx)
            for w in sentences[removed]:
                cover_count[w] -= 1

    return selected
