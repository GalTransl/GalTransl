import json, time, asyncio, os, traceback, re
from turtle import title
from opencc import OpenCC
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from alive_progress import alive_bar
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.ConfigHelper import CProxyPool
from GalTransl import LOGGER, LANG_SUPPORTED
from GalTransl.i18n import get_text, GT_LANG
from sys import exit
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Dictionary import CGptDict
from GalTransl.Utils import contains_katakana, is_all_chinese, decompress_file_lzma
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.Backend.Prompts import GENDIC_PROMPT, GENDIC_SYSTEM, H_WORDS_LIST
import collections
from typing import List, Set, Dict, Optional
from threading import Lock
from GalTransl.TerminalOutput import should_print_translation_logs, terminal_progress

# 正则补充层：连续片假名字串（含・）
_KATAKANA_SEQ_RE = re.compile(r"[ァ-ヶー・]{2,}")


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


class GenDic(BaseTranslate):
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
        proxy_pool: Optional[CProxyPool],
        token_pool: COpenAITokenPool,
    ):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.dic_counter = collections.Counter()
        self.dic_list = []
        self.dic_votes = collections.defaultdict(collections.Counter)
        self.wokers = config.getKey("workersPerProject")
        self.counter_lock = Lock()
        self.list_lock = Lock()
        self.progress_lock = Lock()
        self.progress_display_name = "GenDic 术语提取"
        self.progress_cache_key = "gendic_progress"
        self.progress_append_path = ""
        self.trans_prompt = ""
        self.init_chatbot(eng_type, config)
        pass

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

        self._update_runtime(
            stage="GenDic 术语提取中",
            current_file="准备生成任务",
            workers_active=0,
            workers_configured=int(self.wokers or 1),
            file_totals={self.progress_display_name: int(total_tasks)},
            cache_file_display_map={self.progress_cache_key: self.progress_display_name},
        )

    def _append_runtime_progress(self, task_index: int, success: bool, message: str = ""):
        if not self.progress_append_path:
            return
        entry = {
            "__cache_key": f"gendic-task-{int(task_index)}",
            "pre_dst": "OK" if success else "(Failed)",
            "problem": "" if success else (message or "GenDic 任务失败"),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self.progress_lock:
            with open(self.progress_append_path, "a", encoding="utf-8") as fp:
                fp.write(line)
                fp.write("\n")

    def _cleanup_runtime_progress(self):
        if not self.progress_append_path:
            return
        try:
            if os.path.exists(self.progress_append_path):
                os.remove(self.progress_append_path)
        except Exception:
            pass
        finally:
            self.progress_append_path = ""

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
                trans_by="GenDic",
            )
        except Exception:
            return

    async def llm_gen_dic(self, text: str, name_list=[], task_index: int = 0):
        self._raise_if_stop_requested()
        hint = "无"
        name_hit = []
        for name in name_list:
            self._raise_if_stop_requested()
            if name in text:
                name_hit.append(name)

        parts: List[str] = []
        existing_dict_map = getattr(self, "existing_dict_map", None) or {}
        if existing_dict_map:
            appeared = {
                k: v for k, v in existing_dict_map.items()
                if k in text
            }
            if appeared:
                lines = [f"{src}\t{dst}\t{note}" for src, (dst, note) in appeared.items()]
                parts.append("以下词汇已有确定翻译，请严格保持一致，不要重复提取：\n" + "\n".join(lines))
        if name_hit:
            parts.append("输入文本中的这些词语是一定要加入术语表的: \n" + "\n".join(name_hit))
        if parts:
            hint = "\n\n".join(parts)

        prompt = GENDIC_PROMPT.format(input=text, hint=hint)

        valid_entries = []
        for attempt in range(3):
            self._raise_if_stop_requested()
            try:
                rsp, token = await self.ask_chatbot(
                    prompt=prompt, system=GENDIC_SYSTEM
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 2:
                    LOGGER.error(
                        f"GenDic 分片 {task_index} LLM请求失败，已重试3次，放弃该分片: {e}"
                    )
                    return
                continue

            if should_print_translation_logs(self.pj_config):
                print(rsp)

            lines = rsp.split("\n")
            local_valid = []
            for line in lines:
                self._raise_if_stop_requested()
                sp = line.split("\t")
                if len(sp) < 3:
                    continue
                if "日文" in sp[0]:
                    continue
                src = sp[0].strip()
                dst = sp[1].strip()
                note = sp[2].strip()
                if not src or not dst:
                    continue
                if src == "NULL" and dst == "NULL":
                    return
                local_valid.append((src, dst, note))

            if local_valid:
                valid_entries = local_valid
                break
            else:
                if attempt == 2:
                    LOGGER.warning(
                        f"GenDic 分片 {task_index} 连续3次未解析到有效词条，放弃该分片"
                    )
                    return

        for idx, (src, dst, note) in enumerate(valid_entries):
            if idx < 3:
                self._record_runtime_success(
                    index=task_index,
                    source_preview=src,
                    translation_preview=f"{dst}｜{note}",
                )
            with self.counter_lock:
                self.dic_counter[src] += 1
                self.dic_votes[src][(dst, note)] += 1
                if self.dic_counter[src] == 1:
                    with self.list_lock:
                        self.dic_list.append([src, dst, note])
                elif self.dic_counter[src] == 2:
                    if should_print_translation_logs(self.pj_config):
                        print(f"{src}\t{dst}\t{note}")

    async def batch_translate(
        self,
        json_list: list,
    ) -> bool:
        self._raise_if_stop_requested()
        self._update_runtime(stage="GenDic 分词处理中", current_file="准备分词")

        try:
            with terminal_progress(should_print_translation_logs(self.pj_config), title="载入分词……") as bar:
                # get tmp dir
                import tempfile

                tmp_dir = tempfile.gettempdir()
                model_path = os.path.join(tmp_dir, "bccwj-suw+unidic_pos+pron.model")
                if not os.path.exists(model_path):
                    zst_path = "./res/bccwj-suw+unidic_pos+pron.model.xz"
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

                word_counter = collections.Counter()
                segment_list = []
                segment_words_list = []
                name_set = set()
                max_len = 512
                tmp_text = ""
                for item in json_list:
                    self._raise_if_stop_requested()
                    if len(tmp_text) > max_len:
                        segment_list.append(tmp_text)
                        tmp_text = ""

                    if "name" in item and item["name"] != "":
                        name_set.add(item["name"])
                        tmp_text += item["name"] + item["message"] + "\n"
                        word_counter[item["name"]] += 2
                    else:
                        tmp_text += item["message"] + "\n"

                segment_list.append(tmp_text)
                bar.title = "处理分词……"

                # 收集已有字典翻译，用于去重与提示
                existing_dict_map: Dict[str, Tuple[str, str]] = {}
                for attr in ("gpt_dic", "pre_dic", "post_dic"):
                    dic_obj = getattr(self.pj_config, attr, None)
                    if dic_obj is None:
                        continue
                    dic_list = getattr(dic_obj, "dic_list", None) or getattr(dic_obj, "_dic_list", None) or []
                    for dic in dic_list:
                        if dic.search_word and dic.replace_word:
                            note = getattr(dic, "note", "") or ""
                            existing_dict_map[dic.search_word] = (dic.replace_word, note)
                self.existing_dict_map = existing_dict_map
                all_text = "\n".join(segment_list)

                for item in segment_list:
                    self._raise_if_stop_requested()
                    tmp_words = set()
                    tokens = tokenizer.tokenize(item)
                    for token in tokens:
                        self._raise_if_stop_requested()
                        surf = token.surface()
                        tag = token.tag(0)
                        if len(surf) <= 1:
                            continue
                        if is_all_chinese(surf):
                            continue
                        if tag is None:
                            if contains_katakana(surf):
                                tmp_words.add(surf)
                                word_counter[surf] += 1

                    # 正则补充层：片假名序列、引号/括号内词组
                    for w in _extract_regex_terms(item):
                        tmp_words.add(w)
                        word_counter[w] += 1

                    # 名字强制保留到 Set Cover（确保仅出现一次的名字也被覆盖）
                    for name in name_set:
                        if name in item and len(name) >= 2:
                            tmp_words.add(name)
                            if word_counter[name] < 2:
                                word_counter[name] += 1

                    segment_words_list.append(tmp_words)
                    bar()

            # 放宽过滤：名字和纯片假名词允许出现1次，其他仍需>=2
            word_counter = {
                word: count for word, count in word_counter.items()
                if count >= 2 or word in name_set or _is_katakana_only(word)
            }
            segment_words_list_new = []
            for item in segment_words_list:
                self._raise_if_stop_requested()
                item_new = set()
                for word in item:
                    if word in word_counter:
                        item_new.add(word)
                segment_words_list_new.append(item_new)

            index_list = solve_sentence_selection(segment_words_list_new, max_select=128, name_set=name_set)
            self._prepare_runtime_progress(len(index_list))
            LOGGER.info(f"启动{self.wokers}个工作线程，共{len(index_list)}个任务")
            sem = asyncio.Semaphore(self.wokers)
            completed_tasks = 0

            async def process_item_async(idx):
                async with sem:
                    self._raise_if_stop_requested()
                    try:
                        item = segment_list[idx]
                        await self.llm_gen_dic(item, name_list=list(name_set), task_index=idx)
                        return idx, True, ""
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        from GalTransl.Service import JobCancelledError

                        if isinstance(e, JobCancelledError):
                            raise
                        LOGGER.error(f"处理任务时出错: {e}")
                        return idx, False, str(e)

            tasks = [asyncio.create_task(process_item_async(idx)) for idx in index_list]
            with terminal_progress(
                should_print_translation_logs(self.pj_config),
                total=len(index_list), title=f"{self.wokers} 线程生成字典中……"
            ) as bar:
                self.pj_config.bar = bar
                self._update_runtime(
                    stage="GenDic 术语提取中",
                    current_file="开始并发生成",
                    workers_active=int(self.wokers or 1),
                )
                try:
                    for f in asyncio.as_completed(tasks):
                        self._raise_if_stop_requested()
                        idx, ok, error_message = await f
                        completed_tasks += 1
                        self._append_runtime_progress(idx, ok, error_message)
                        self._update_runtime(
                            stage="GenDic 术语提取中",
                            current_file=f"已完成 {completed_tasks}/{len(index_list)}",
                            workers_active=int(self.wokers or 1),
                        )
                        bar()
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

            self.dic_list.sort(key=lambda x: self.dic_counter[x[0]], reverse=True)

            # 用多数投票结果覆盖 dic_list 中的翻译和备注
            for i in range(len(self.dic_list)):
                src = self.dic_list[i][0]
                if src in self.dic_votes and self.dic_votes[src]:
                    (best_dst, best_note), _ = self.dic_votes[src].most_common(1)[0]
                    self.dic_list[i][1] = best_dst
                    self.dic_list[i][2] = best_note

            # 最终列表：先合并已有字典翻译（出现在当前文本中的），再合并 LLM 提取结果
            final_set: Dict[str, List[str]] = {}
            for src, (dst, note) in getattr(self, "existing_dict_map", {}).items():
                if src in all_text:
                    final_set[src] = [src, dst, note or ""]

            for item in self.dic_list:
                src = item[0]
                if src in final_set:
                    continue
                if "NULL" in src:
                    continue
                if src in H_WORDS_LIST:
                    continue
                if "（" not in src and "（" in item[1]:
                    continue

                if self.dic_counter[src] > 1:
                    final_set[src] = item
                elif "人名" in item[2]:
                    final_set[src] = item
                elif "地名" in item[2]:
                    final_set[src] = item
                elif src in word_counter:
                    final_set[src] = item
                elif src in name_set:
                    final_set[src] = item

            final_list = list(final_set.values())
            result_path = os.path.join(self.pj_config.getProjectDir(), "项目GPT字典-生成.txt")

            with open(result_path, "w", encoding="utf-8") as f:
                f.write("# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki\n")
                for item in final_list:
                    f.write(item[0] + "\t" + item[1] + "\t" + item[2] + "\n")
            LOGGER.info(f"字典生成完成，共{len(final_list)}个词语，保存到{result_path}")
            self._update_runtime(stage="", current_file="", workers_active=0)

            return True
        finally:
            self._cleanup_runtime_progress()


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
