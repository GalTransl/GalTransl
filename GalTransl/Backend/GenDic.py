import json, time, asyncio, os, traceback
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
        self.wokers = config.getKey("workersPerProject")
        self.counter_lock = Lock()
        self.list_lock = Lock()
        self.progress_lock = Lock()
        self.progress_display_name = "GenDic 术语提取"
        self.progress_cache_key = "gendic_progress"
        self.progress_append_path = ""
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

        if name_hit:
            hint = "输入文本中的这些词语是一定要加入术语表的: \n" + "\n".join(name_hit)

        prompt = GENDIC_PROMPT.format(input=text, hint=hint)
        rsp, token = await self.ask_chatbot(
            prompt=prompt, system=GENDIC_SYSTEM, temperature=0.6
        )
        if should_print_translation_logs(self.pj_config):
            print(rsp)
        lines = rsp.split("\n")
        runtime_preview_count = 0
        runtime_preview_limit = 3

        for line in lines:
            self._raise_if_stop_requested()
            sp = line.split("\t")
            if len(sp) < 3:
                continue

            if "日文" in sp[0]:
                continue
            src = sp[0]
            dst = sp[1]
            note = sp[2]
            if runtime_preview_count < runtime_preview_limit:
                self._record_runtime_success(
                    index=task_index,
                    source_preview=src,
                    translation_preview=f"{dst}｜{note}",
                )
                runtime_preview_count += 1
            with self.counter_lock:
                if src in self.dic_counter:
                    self.dic_counter[src] += 1
                    if self.dic_counter[src] == 2:
                        if should_print_translation_logs(self.pj_config):
                            print(f"{src}\t{dst}\t{note}")
                else:
                    self.dic_counter[src] = 1
                    with self.list_lock:
                        self.dic_list.append([src, dst, note])

    async def batch_translate(
        self,
        json_list: list,
    ) -> bool:
        self._raise_if_stop_requested()
        self._update_runtime(stage="GenDic 分词处理中", current_file="准备分词")

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
                segment_words_list.append(tmp_words)
                bar()

        # 剔除出现次数小于2的词语
        word_counter = {
            word: count for word, count in word_counter.items() if count >= 2
        }
        segment_words_list_new = []
        for item in segment_words_list:
            self._raise_if_stop_requested()
            item_new = set()
            for word in item:
                if word in word_counter:
                    item_new.add(word)
            segment_words_list_new.append(item_new)

        index_list = solve_sentence_selection(segment_words_list_new)
        # 取前100个
        index_list = index_list[:128]
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

        # 保存到文件
        # 按出现次数排序
        self.dic_list.sort(key=lambda x: self.dic_counter[x[0]], reverse=True)
        final_list = []
        # 过滤只出现1次的词语
        for item in self.dic_list:
            self._raise_if_stop_requested()
            if "NULL" in item[0]:
                continue

            if item[0] in H_WORDS_LIST:
                continue
            if "（" not in item[0] and "（" in item[1]:
                continue

            if self.dic_counter[item[0]] > 1:
                final_list.append(item)
            elif "人名" in item[2]:
                final_list.append(item)
            elif "地名" in item[2]:
                final_list.append(item)
            elif item[0] in word_counter:
                final_list.append(item)
            elif item[0] in name_set:
                final_list.append(item)

        result_path = os.path.join(self.pj_config.getProjectDir(), "项目GPT字典-生成.txt")

        with open(result_path, "w", encoding="utf-8") as f:
            f.write("# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki\n")
            for item in final_list:
                f.write(item[0] + "\t" + item[1] + "\t" + item[2] + "\n")
        LOGGER.info(f"字典生成完成，共{len(final_list)}个词语，保存到{result_path}")
        self._update_runtime(stage="", current_file="", workers_active=0)

        return True


def solve_sentence_selection(sentences):
    all_words = set()
    for sentence in sentences:
        all_words.update(sentence)

    covered_words = set()
    selected_indices = []
    remaining_sentences_indices = list(range(len(sentences)))

    while covered_words != all_words and remaining_sentences_indices:
        best_sentence_index = -1
        max_new_coverage = -1

        for index in remaining_sentences_indices:
            sentence = sentences[index]
            new_coverage = len(sentence - covered_words)

            if new_coverage > max_new_coverage:
                max_new_coverage = new_coverage
                best_sentence_index = index

        if best_sentence_index != -1:
            best_sentence = sentences[best_sentence_index]
            covered_words.update(best_sentence)
            selected_indices.append(best_sentence_index)
            remaining_sentences_indices.remove(best_sentence_index)
        else:
            break

    return selected_indices
