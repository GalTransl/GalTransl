"""GenDic 生成 GPT 字典的流程：人名预译 → 分片提取 → 联合终审（筛选与译名一致性）。

以前的几个老毛病，这里各锁一条：

1. 说话人名一律当人名塞进术语表，产出过「みんな→米娜（人名）」「全員（人名）」这种翻译时有害的词条
   ——现在先用分词判断说话人名像不像角色名；
2. 分片是按集合覆盖挑的，一个词通常只在一个分片里被看到，「被提取两次以上才保留」的过滤把
   和菓子開発部、風紀強制実行委員会 这类只露面一两次的专有名词成批丢掉——现在改为汇总后整体审校，
   审校没覆盖到的才按规则兜底；
3. 各分片各译各的，同一个角色的名字和全名译法对不上——审校时把互为子串、共用姓氏的词条放同一批；
4. 台词里的换行会把送审表格拆乱——整理文本时一句压成一行；
5. 每个分片都单独开一次会话，说明、格式、已有词条反反复复地发——现在连续的几个分片交给同一个
   会话多轮对话：说明只发一次，后面只发新片段并只要求输出新词，译名也更一致；
6. 多轮对话的历史里，assistant 消息要带上当初的思考内容（thinking 模式下少一条
   reasoning_content，下一次请求会被判 400）；
7. 工作台的「实时速度/预计剩余」按「最近一分钟的成功事件数」算，而 GenDic 的成功事件是
   抽出来的术语（一段几十个），拿它估 130 项分片会说「还剩 2 分钟」——现在按完成项数/耗时
   报一个与进度同口径的速度（项/分），收尾清零。
"""

import asyncio
import collections
import os
import tempfile
import time
import unittest
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Backend.BaseTranslate import _extract_reasoning
from GalTransl.Backend.GenDic import (
    GenDic,
    _category_of,
    _ChatSession,
    _example_lines,
    _is_probable_character_name,
    _merge_into_sections,
    _pack_review_batches,
    _plan_sessions,
    _plan_name_batches,
    _SESSION_MAX_TURNS,
    _split_segments,
)


class _Token:
    def __init__(self, surface, tag):
        self._surface = surface
        self._tag = tag

    def surface(self):
        return self._surface

    def tag(self, _index):
        return self._tag


class _FakeTokenizer:
    """按预设切分返回词和词性；没登记的词整体当一个未登录词（tag 为 None）。"""

    def __init__(self, table):
        self.table = table

    def tokenize(self, text):
        return [_Token(s, t) for s, t in self.table.get(text, [(text, None)])]


class _StubConfig:
    stop_event = None
    non_interactive = True  # 不往终端打印模型输出

    def getProjectDir(self):
        return "."


def _make_engine():
    """不跑 __init__（它要连模型接口），只装上审校/汇总用到的状态。"""
    engine = GenDic.__new__(GenDic)
    engine.pj_config = _StubConfig()
    engine.dic_votes = collections.defaultdict(collections.Counter)
    engine.note_votes = collections.defaultdict(collections.Counter)
    engine.review_decisions = {}
    engine.name_decisions = {}
    engine.counter_lock = Lock()
    engine.progress_display_name = "GenDic 术语提取"
    engine.progress_lock = Lock()
    engine.progress_append_path = ""
    engine.progress_done = 0
    engine.progress_started_at = 0.0
    engine.gendic_max_api_retries = 2
    engine.wokers = 4
    engine._record_runtime_error = lambda **kwargs: None
    engine._record_runtime_success = lambda **kwargs: None
    return engine


def _candidate(src, count, dst, note="", votes=1):
    return {"src": src, "dsts": [dst], "note": note, "count": count, "votes": votes}


class CharacterNameHeuristicTests(unittest.TestCase):
    def setUp(self):
        self.tokenizer = _FakeTokenizer(
            {
                "みんな": [("みんな", "名詞-普通名詞-副詞可能")],
                "全員": [("全員", "名詞-普通名詞-副詞可能")],
                "女の子Ａ": [("女の子", "名詞-普通名詞-一般"), ("Ａ", "記号-文字")],
                "？？？": [("？", "補助記号-句点")] * 3,
                "ゴブリン": [("ゴブリン", "名詞-普通名詞-一般")],
                "アリス": [("アリス", "名詞-固有名詞-人名-一般")],
                # 片假名名字被切成奇怪的几段：不是常见词
                "サヴィーネ": [("サ", "接尾辞-名詞的-一般"), ("ヴィー", "名詞-普通名詞-一般"), ("ネ", "助詞-終助詞")],
                "玲": [("玲", "名詞-固有名詞-人名-名")],
                "母": [("母", "名詞-普通名詞-一般")],
            }
        )
        self.name_set = {
            "みんな", "全員", "女の子Ａ", "？？？", "ゴブリン", "アリス", "サヴィーネ", "炎輝", "瑠那",
            "瑠那・炎輝", "咲來", "$str20", "玲", "母",
        }

    def check(self, name):
        return _is_probable_character_name(name, self.tokenizer, self.name_set)

    def test_generic_speaker_labels_are_not_names(self):
        for name in ("みんな", "全員", "女の子Ａ", "？？？", "ゴブリン"):
            self.assertFalse(self.check(name), name)

    def test_character_names(self):
        # $str20 这类说话人变量一般是主角名字的占位符，要进术语表让模型知道它指谁
        for name in ("アリス", "サヴィーネ", "炎輝", "咲來", "$str20"):
            self.assertTrue(self.check(name), name)

    def test_single_char_name_needs_dictionary_person_tag(self):
        self.assertTrue(self.check("玲"))
        self.assertFalse(self.check("母"))

    def test_multi_speaker_label_is_not_one_character(self):
        self.assertFalse(self.check("瑠那・炎輝"))


class SegmentAndExampleTests(unittest.TestCase):
    def test_segments_never_split_a_line(self):
        lines = [f"line{i}" + "あ" * 40 for i in range(30)]
        segments = _split_segments(lines, 200)
        self.assertGreater(len(segments), 1)
        self.assertEqual("\n".join(segments).split("\n"), lines)

    def test_examples_keep_speaker_when_trimmed(self):
        lines = ["短い行", "アリス：" + "あ" * 80 + "ラビア公国" + "い" * 80]
        all_text = "\n".join(lines)
        starts = [0, len(lines[0]) + 1]
        [example] = _example_lines(all_text, starts, ["", "アリス"], "ラビア公国", 1)
        self.assertTrue(example.startswith("アリス："))
        self.assertIn("ラビア公国", example)
        self.assertLess(len(example), len(lines[1]))
        self.assertEqual(_example_lines(all_text, starts, ["", "アリス"], "短い", 1), ["短い行"])

    def test_examples_come_from_first_and_middle_occurrence(self):
        # 只看第一处常看不出这个词一般怎么用（キノコ第一次出现是外号，其余都是蘑菇）
        lines = ["キノコ先生", "キノコ狩り", "キノコ鍋", "焼きキノコ", "キノコの山"]
        all_text = "\n".join(lines)
        starts = [all_text.index(line) for line in lines]
        examples = _example_lines(all_text, starts, [""] * 5, "キノコ", 5)
        self.assertEqual(examples, ["キノコ先生", "キノコ鍋"])


class ReviewBatchPackingTests(unittest.TestCase):
    def test_nickname_is_grouped_by_character_in_note(self):
        candidates = [
            _candidate("トレニア", 800, "托蕾妮亚", "人名，女性"),
            _candidate("白鷺市", 700, "白鹭市", "地名"),
            _candidate("トレニャン", 2, "特蕾喵", "称呼，トレニア的爱称"),
        ]
        batches = _pack_review_batches(candidates, 2)
        self.assertEqual({c["src"] for c in batches[0]}, {"トレニア", "トレニャン"})

    def test_related_terms_share_a_batch(self):
        candidates = [_candidate(f"用語{i:02d}", 100 - i, f"术语{i}") for i in range(10)]
        candidates += [
            _candidate("ギルティア", 90, "吉尔蒂亚"),
            _candidate("ギルティア・ル・ドルード", 1, "吉尔蒂亚·勒·多鲁德"),
            _candidate("サヴィーネ・ル・ドルード", 1, "萨维涅·勒·多鲁德"),
            _candidate("サヴィーネ", 30, "萨维涅"),
        ]
        batches = _pack_review_batches(candidates, 6)
        family = {"ギルティア", "ギルティア・ル・ドルード", "サヴィーネ・ル・ドルード", "サヴィーネ"}
        holding = [b for b in batches if family & {c["src"] for c in b}]
        self.assertEqual(len(holding), 1)
        self.assertTrue(family <= {c["src"] for c in holding[0]})
        self.assertEqual(sorted(c["src"] for b in batches for c in b), sorted(c["src"] for c in candidates))

    def test_oversized_group_is_split(self):
        candidates = [_candidate("メア" + "ア" * i, 10, "梅亚") for i in range(30)]
        batches = _pack_review_batches(candidates, 5)
        self.assertTrue(all(len(b) <= 5 for b in batches))
        self.assertEqual(sum(len(b) for b in batches), 30)


class CandidateAndFinalListTests(unittest.TestCase):
    def test_collect_candidates_checks_text_and_known_terms(self):
        engine = _make_engine()
        engine.dic_votes["和菓子開発部"]["和菓子开发部"] += 1
        engine.dic_votes["瑠那"]["瑠那"] += 3
        engine.dic_votes["瑠那"]["琉那"] += 1
        engine.note_votes["瑠那"]["人名，女性"] += 2
        engine.dic_votes["存在しない語"]["不存在"] += 2  # 模型编造/改写过的词
        engine.dic_votes["アリス"]["爱丽丝"] += 1  # 已在别的 GPT 字典里
        text = "瑠那：「和菓子開発部よ」\n瑠那：「アリスも」"
        candidates, duplicates = engine._collect_candidates(text, {"アリス"})
        self.assertEqual([c["src"] for c in candidates], ["瑠那", "和菓子開発部"])
        self.assertEqual(candidates[0]["dsts"][0], "瑠那")
        self.assertEqual(candidates[0]["note"], "人名，女性")
        self.assertEqual(duplicates, 1)

    def test_collect_candidates_drops_common_words_lines_and_stray_chars(self):
        engine = _make_engine()
        tokenizer = _FakeTokenizer(
            {
                "キノコ": [("キノコ", "名詞-普通名詞-一般")],
                "ルナ": [("ルナ", "名詞-普通名詞-一般")],
                "玲": [("玲", "名詞-固有名詞-人名-名")],
                "奏": [("奏", "名詞-普通名詞-一般")],
            }
        )
        votes = {
            "キノコ": ("蘑菇", "外号"),  # 分词词典里的普通词：不收
            "ルナ": ("露娜", "人名，女性"),  # 也是普通词，但备注说是人名：留给审校
            "玲": ("玲", "人名，女性"),  # 单字，但是角色名
            "奏": ("奏", "人名，女性"),  # 单字的说话人名，分词只当普通词：交给审校
            "$str20": ("$str20", "主角名字的变量"),  # 变量保留，让模型知道它指谁
            "レ": ("蕾", "名字中间的连接词"),  # 单字：不收
            "突破せよ！": ("突破吧！", "台词"),  # 台词：不收
        }
        for src, (dst, note) in votes.items():
            engine.dic_votes[src][dst] += 1
            engine.note_votes[src][note] += 1
        text = "玲：「キノコ狩りよ、ルナ。突破せよ！」ラエルダ・レ・ファイルーダ\n奏：「ええ」\n$str20：「うん」"
        candidates, _ = engine._collect_candidates(text, set(), {"玲", "奏"}, {"玲"}, tokenizer)
        self.assertEqual(sorted(c["src"] for c in candidates), ["$str20", "ルナ", "奏", "玲"])

    def test_review_decisions_and_fallback(self):
        engine = _make_engine()
        engine.review_decisions = {
            "みんな": None,
            "ラエルダ": ("拉埃尔达", "人名，女性"),
        }
        candidates = [
            _candidate("みんな", 128, "米娜", "人名"),
            _candidate("ラエルダ", 152, "拉艾尔达", "人名，女性"),
            # 以下没审校到：按规则兜底
            _candidate("全員", 31, "全员", "人名"),  # 泛称说话人名：不收
            _candidate("風紀強制実行委員会", 1, "风纪强制执行委员会", "组织"),  # 只出现一次、备注不是名字：不收
            _candidate("ビキニ騎士団", 3, "比基尼骑士团", "组织"),
        ]
        final = engine._build_final_list(candidates, {"みんな", "全員", "ラエルダ"}, {"ラエルダ"})
        self.assertEqual(
            final,
            [["ラエルダ", "拉埃尔达", "人名，女性"], ["ビキニ騎士団", "比基尼骑士团", "组织"]],
        )


class ReviewBatchParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_output_is_applied(self):
        engine = _make_engine()
        replies = []

        async def fake_ask(prompt, task_label, task_index, session=None):
            replies.append(prompt)
            return (
                "```tsv\n日文原词\t中文翻译\t备注\n"
                "ラエルダ\t拉埃尔达\t人名，女性\n"
                "ラエルダ・レ・ファイルーダ\t拉埃尔达·蕾·菲鲁达\t人名，ラエルダ的全名\n"
                "みんな\tDELETE\t泛称\n"
                "别的词\t不在这一批\t\n"
                "```"
            )

        engine._ask_gendic = fake_ask
        batch = [
            _candidate("ラエルダ", 152, "拉艾尔达", "人名，女性"),
            _candidate("ラエルダ・レ・ファイルーダ", 1, "拉艾尔达·蕾·法伊鲁达", "人名"),
            _candidate("みんな", 128, "米娜", "人名"),
        ]
        text = "ラエルダ：「ラエルダ・レ・ファイルーダだ」\nみんな：「おー」"
        ok = await engine._review_batch(batch, 0, text, [0, text.index("\n") + 1], ["ラエルダ", "みんな"], {"ファイルーダ": ("菲鲁达", "家名")})
        self.assertTrue(ok)
        self.assertEqual(engine.review_decisions["ラエルダ"], ("拉埃尔达", "人名，女性"))
        self.assertEqual(engine.review_decisions["ラエルダ・レ・ファイルーダ"][0], "拉埃尔达·蕾·菲鲁达")
        self.assertIsNone(engine.review_decisions["みんな"])
        self.assertNotIn("别的词", engine.review_decisions)
        # 相关的已有译名作为参考发给审校
        self.assertIn("ファイルーダ\t菲鲁达\t家名", replies[0])

    async def test_failed_review_leaves_batch_to_fallback(self):
        engine = _make_engine()

        async def fake_ask(prompt, task_label, task_index, session=None):
            return None

        engine._ask_gendic = fake_ask
        ok = await engine._review_batch([_candidate("ラエルダ", 152, "拉艾尔达")], 0, "ラエルダ", [0], [""], {})
        self.assertFalse(ok)
        self.assertEqual(engine.review_decisions, {})


class NameConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_compounds_and_nickname_are_corrected_before_saving(self):
        engine = _make_engine()
        engine._update_runtime = lambda **kwargs: None
        events = []
        engine._append_runtime_progress = lambda *args: events.append(args)
        final = [
            ["クローディア", "克罗迪娅", "人名，女性"],
            ["サフィア・クローディア", "萨菲亚·克劳迪娅", "人名，全名"],
            ["クローディア？", "克劳迪娅？", "称呼，疑问说话人"],
            ["クローディア・灯矢", "克劳迪娅·灯矢", "称呼，合并说话人"],
            ["トレニャン", "特蕾喵", "称呼，トレニア的爱称"],
            ["クラウツェル", "克劳采尔", "人名，另一角色的姓氏"],
            ["白鷺市", "白鹭市", "地名"],
        ]
        corrected = {
            "サフィア・クローディア": "萨菲亚·克罗迪娅",
            "クローディア？": "克罗迪娅？",
            "クローディア・灯矢": "克罗迪娅·灯矢",
            "トレニャン": "托蕾喵",
        }
        known = {"トレニア": ("托蕾妮亚", "人名，女性")}
        prompts = []

        async def fake_ask(prompt, task_label, task_index, session=None):
            prompts.append(prompt)
            return "\n".join(
                "\t".join([src, corrected.get(src, dst), note])
                for src, dst, note in final if src != "白鷺市"
            ) + "\nローション\tDELETE\t普通词\n知らない名前\t编造的名字\t人名"

        engine._ask_gendic = fake_ask
        text = "クローディア：トレニャン、来て。"
        name_set = {"クローディア", "クローディア？", "クローディア・灯矢"}
        engine.name_decisions = {src: (dst, note) for src, dst, note in final if src in name_set}
        candidates = [_candidate(src, 1, dst, note) for src, dst, note in final if src not in name_set]
        candidates.append(_candidate("ローション", 1, "润滑液", "物品"))
        await engine._run_final_review(candidates, name_set, known, text, [0], ["クローディア"])
        final = engine._build_final_list(candidates, name_set, name_set, text)
        self.assertEqual(len(prompts), 1)
        # 没有子串关系的主名也必须送作参考；全表含不同角色的姓氏，供模型判别。
        self.assertIn("トレニア\t托蕾妮亚", prompts[0])
        self.assertIn("クラウツェル\t克劳采尔", prompts[0])
        self.assertIn("白鷺市\t白鹭市", prompts[0])
        self.assertEqual(known, {"トレニア": ("托蕾妮亚", "人名，女性")})
        self.assertEqual(events[0][1], True)
        self.assertEqual(len(final), 7)
        with tempfile.TemporaryDirectory() as tmp:
            path = engine._save_generated_dictionary(final, os.path.join(tmp, "字典.txt"))
            saved = engine._load_existing_generated_terms(path)
        for src, dst in corrected.items():
            self.assertEqual(saved[src][0], dst)
        self.assertEqual(saved["クローディア"][0], "克罗迪娅")
        self.assertEqual(saved["クラウツェル"][0], "克劳采尔")
        self.assertEqual(saved["白鷺市"][0], "白鹭市")

    async def test_incomplete_or_invalid_batch_never_partially_changes_names(self):
        engine = _make_engine()
        original = [["クローディア", "克劳迪娅", "人名"], ["クローディア？", "克劳迪娅？", "称呼"]]
        batch = [_candidate(src, 1, dst, note) for src, dst, note in original]
        good = "クローディア\t克罗迪娅\t人名\nクローディア？\t克罗迪娅？\t称呼"
        replies = [
            None,
            "クローディア\t克罗迪娅\t人名",  # 漏复合行，不提交主行的修改
            "クローディア\t克罗迪娅\t人名\nクローディア？\tDELETE\t称呼",
            "クローディア\t克罗迪娅\t人名\nクローディア？\tNULL\t称呼",
            good + "\nクローディア\t另一译名\t人名",
            "クローディア\t另一译名\t人名\nクローディア？\t另一译名？\t称呼",
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                async def fake_ask(*args):
                    return reply

                engine._ask_gendic = fake_ask
                ok = await engine._review_batch(
                    batch, 0, "クローディア？", [0], [""], {"クローディア": ("克罗迪娅", "人名")},
                    protected_names={row[0] for row in original},
                )
                self.assertFalse(ok)
                self.assertEqual(engine.review_decisions, {})

    async def test_independent_batches_run_concurrently_with_fixed_reference(self):
        engine = _make_engine()
        engine.wokers = 2
        engine._update_runtime = lambda **kwargs: None
        engine._append_runtime_progress = lambda *args: None
        engine.name_decisions = {"トレニア": ("托蕾妮亚", "人名")}
        candidates = [
            _candidate("トレニャン", 1, "特蕾喵", "称呼"),
            _candidate("白ネコ", 1, "白猫", "称呼"),
        ]
        prompts = []
        active = peak = 0
        both_started = asyncio.Event()

        async def fake_ask(prompt, *args):
            nonlocal active, peak
            prompts.append(prompt)
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            try:
                await asyncio.wait_for(both_started.wait(), timeout=1)
                src = prompt.split("## 候选词条\n", 1)[1].split("\t", 1)[0]
                # 即使主名那批擅自换译法，也不能污染其他批次共用的基准。
                dst = {"トレニア": "特蕾妮亚", "トレニャン": "托蕾喵", "白ネコ": "白猫"}[src]
                return f"{src}\t{dst}\t称呼"
            finally:
                active -= 1

        engine._ask_gendic = fake_ask
        with patch("GalTransl.Backend.GenDic._REVIEW_BATCH_SIZE", 1):
            await engine._run_final_review(candidates, {"トレニア"}, {}, "トレニア トレニャン 白ネコ", [0], [""])
        self.assertEqual(peak, 2)
        self.assertEqual(len(prompts), 3)
        self.assertNotIn("トレニア", engine.review_decisions)  # 擅改固定主名的批次拒绝提交
        self.assertEqual(engine.name_decisions["トレニア"][0], "托蕾妮亚")
        self.assertEqual(engine.review_decisions["トレニャン"][0], "托蕾喵")
        references = [p.split("## 本批必须保留的说话人", 1)[0] for p in prompts]
        self.assertEqual(references, [references[0]] * 3)

    async def test_stop_cancels_other_reviews_and_keeps_completed_results(self):
        from GalTransl.Service import JobCancelledError

        engine = _make_engine()
        engine.wokers = 2
        engine._update_runtime = lambda **kwargs: None
        completed = asyncio.Event()
        last_started = asyncio.Event()
        last_cancelled = []
        engine._append_runtime_progress = lambda *args: completed.set()
        candidates = [
            _candidate("ひとつ", 3, "一", "称呼"),
            _candidate("ふたつ", 2, "二", "称呼"),
            _candidate("みっつ", 1, "三", "称呼"),
        ]

        async def fake_ask(prompt, label, index, session=None):
            if index == 0:
                return "ひとつ\t一号\t称呼"
            if index == 1:
                await asyncio.wait_for(completed.wait(), timeout=1)
                await asyncio.wait_for(last_started.wait(), timeout=1)
                raise JobCancelledError()
            last_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                last_cancelled.append(True)
                raise

        engine._ask_gendic = fake_ask
        with patch("GalTransl.Backend.GenDic._REVIEW_BATCH_SIZE", 1):
            with self.assertRaises(JobCancelledError):
                await engine._run_final_review(candidates, set(), {}, "ひとつ ふたつ みっつ", [0], [""])
        self.assertEqual(engine.review_decisions, {"ひとつ": ("一号", "称呼")})
        self.assertEqual(last_cancelled, [True])

    async def test_pipeline_proofreads_pretranslated_names_and_preserves_results_on_stop(self):
        from GalTransl.Service import JobCancelledError

        for stop in (False, True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                engine = _make_engine()
                engine.pj_config.getProjectDir = lambda: tmp
                engine.wokers = 1
                engine._update_runtime = lambda **kwargs: None
                engine._prepare_runtime_progress = lambda total: None
                engine._append_runtime_progress = lambda *args: None
                engine._load_existing_gpt_terms = lambda: {}
                engine._build_text_lines = lambda _: (["クローディア：こんにちは"], ["クローディア"], {"クローディア"})
                phases = []

                async def fake_ask(prompt, label, index, session=None):
                    phases.append(label)
                    if label == "人名批次":
                        return "クローディア\t克罗迪娅\t人名，女性"
                    if label == "分片":
                        return "NULL\tNULL\tNULL"
                    if stop:
                        raise JobCancelledError()
                    return "クローディア\t克罗迪娅\t人名，女性，主角"

                engine._ask_gendic = fake_ask
                with open(os.path.join(tmp, "bccwj-suw+unidic_pos+pron.model"), "wb") as f:
                    f.write(b"test model")
                with patch("tempfile.gettempdir", return_value=tmp), patch("vaporetto.Vaporetto", return_value=_FakeTokenizer({})):
                    if stop:
                        with self.assertRaises(JobCancelledError):
                            await engine.batch_translate([])
                        self.assertTrue(engine.pj_config.gendic_partial_saved)
                    else:
                        self.assertTrue(await engine.batch_translate([]))
                self.assertEqual(phases, ["人名批次", "分片", "审校批次"])
                saved = engine._load_existing_generated_terms(os.path.join(tmp, "项目GPT字典-生成.txt"))
                expected_note = "人名，女性" if stop else "人名，女性，主角"
                self.assertEqual(saved["クローディア"], ("克罗迪娅", expected_note))


class ExtractionParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_response_is_retried_once(self):
        # 流式接口偶尔整段返回空、不抛异常；不重试的话这一片里的词就整片丢了
        engine = _make_engine()
        engine.gendic_max_api_retries = 1
        replies = ["", "日文原词\t中文翻译\t备注\n炎輝\t炎辉\t人名，男性"]
        calls = []

        async def fake_ask_chatbot(**kwargs):
            calls.append(kwargs)
            return replies[len(calls) - 1], None

        engine.ask_chatbot = fake_ask_chatbot
        rsp = await engine._ask_gendic("prompt", "分片", 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("炎輝", rsp)

        calls.clear()
        replies[:] = ["", " "]
        self.assertIsNone(await engine._ask_gendic("prompt", "分片", 0))
        self.assertEqual(len(calls), 2)
    async def test_two_column_rows_and_trailing_null(self):
        engine = _make_engine()
        engine.existing_dict_map = {}

        async def fake_ask(prompt, task_label, task_index, session=None):
            return "日文原词\t中文翻译\t备注\nマジホ\t魔机\n炎輝\t炎辉\t人名，男性\nNULL\tNULL\tNULL"

        engine._ask_gendic = fake_ask
        ok = await engine.llm_gen_dic("炎輝：マジホ", name_list=["炎輝"], task_index=0)
        self.assertTrue(ok)
        self.assertEqual(engine.dic_votes["マジホ"]["魔机"], 1)
        self.assertEqual(engine.note_votes["炎輝"]["人名，男性"], 1)


class MultiTurnSessionTests(unittest.IsolatedAsyncioTestCase):
    """提取和审校都按连续片段切会话多轮对话：说明只发一次，后续轮次只发新片段；
    装不下（轮数/字数上限）或这一轮失败就换全新会话按第一轮重发。"""

    def test_plan_groups_consecutive_items_without_losing_order(self):
        items = list(range(37))
        plans = _plan_sessions(items, 5)
        self.assertEqual([i for plan in plans for i in plan], items)  # 顺序不变，一个不漏
        self.assertGreaterEqual(len(plans), 5)  # 会话数不少于并发数
        self.assertTrue(all(len(plan) <= _SESSION_MAX_TURNS for plan in plans))
        # 分片比并发数还少时退化成一轮一个会话
        self.assertEqual(_plan_sessions([1, 2], 8), [[1], [2]])

    def test_session_has_room_counts_turns_and_chars(self):
        session = _ChatSession()
        self.assertTrue(session.has_room(10))
        for _ in range(_SESSION_MAX_TURNS):
            session.add_turn("u", "a")
        self.assertFalse(session.has_room(10))  # 轮数到顶
        session.reset()
        self.assertTrue(session.has_room(10))
        self.assertEqual(session.turns, 0)

    async def test_extraction_followup_uses_short_prompt_and_session_hint(self):
        engine = _make_engine()
        engine.existing_dict_map = {"マジホ": ("魔机", "物品")}
        prompts = []

        async def fake_ask(prompt, task_label, task_index, session=None):
            prompts.append(prompt)
            rsp = "日文原词\t中文翻译\t备注\n瑠那\t瑠那\t人名，女性"
            if session is not None:  # 真 _ask_gendic 成功后会把这轮记进会话
                session.add_turn(prompt, rsp)
            return rsp

        engine._ask_gendic = fake_ask
        session = _ChatSession()
        await engine.llm_gen_dic("瑠那：マジホ", task_index=0, session=session)
        await engine.llm_gen_dic("瑠那：マジホ", task_index=1, session=session)
        self.assertEqual(session.turns, 2)
        self.assertIn("## 任务", prompts[0])  # 第一轮：完整说明
        self.assertNotIn("## 任务", prompts[1])  # 第二轮：简短追问
        self.assertIn("只输出本段新出现的词", prompts[1])
        # 已有词条只在第一轮提示过，第二轮不再重复提示
        self.assertIn("マジホ\t魔机\t物品", prompts[0])
        self.assertNotIn("已有确定翻译", prompts[1])
        self.assertEqual([m["role"] for m in session.messages], ["user", "assistant", "user", "assistant"])

    async def test_full_session_restarts_with_full_prompt(self):
        engine = _make_engine()
        prompts = []

        async def fake_ask(prompt, task_label, task_index, session=None):
            prompts.append(prompt)
            rsp = "日文原词\t中文翻译\t备注\nマジホ\t魔机\t物品"
            if session is not None:
                session.add_turn(prompt, rsp)
            return rsp

        engine._ask_gendic = fake_ask
        session = _ChatSession()
        for _ in range(_SESSION_MAX_TURNS):
            session.add_turn("旧片段", "旧结果")
        session.hinted = {"炎輝"}  # 旧会话里提示过的人名
        await engine.llm_gen_dic("炎輝：マジホ", name_list=["炎輝"], task_index=0, session=session)
        self.assertIn("## 任务", prompts[0])  # 满了就按第一轮重发完整说明
        self.assertIn("以下说话人名是角色名", prompts[0])  # 新会话的提示重新给全
        self.assertEqual(session.turns, 1)

    async def test_review_followup_reuses_session(self):
        engine = _make_engine()
        prompts = []

        async def fake_ask(prompt, task_label, task_index, session=None):
            prompts.append(prompt)
            rsp = "日文原词\t中文翻译\t备注\nラエルダ\t拉埃尔达\t人名，女性"
            if session is not None:
                session.add_turn(prompt, rsp)
            return rsp

        engine._ask_gendic = fake_ask
        session = _ChatSession()
        text = "ラエルダ：「ラエルダだ」"
        starts, speakers = [0], ["ラエルダ"]
        ok = await engine._review_batch(
            [_candidate("ラエルダ", 3, "拉艾尔达")], 0, text, starts, speakers, {}, session
        )
        self.assertTrue(ok)
        ok = await engine._review_batch(
            [_candidate("ラエルダ", 3, "x")], 1, text, starts, speakers, {}, session
        )
        self.assertTrue(ok)
        self.assertEqual(session.turns, 2)
        self.assertIn("## 任务", prompts[0])  # 第一轮：完整审校说明
        self.assertNotIn("## 任务", prompts[1])  # 第二轮：沿用前面的审校要求
        self.assertIn("审校要求和输出格式与前面相同", prompts[1])
        self.assertEqual(engine.review_decisions["ラエルダ"], ("拉埃尔达", "人名，女性"))

    async def test_reasoning_content_is_echoed_back_in_followup_turns(self):
        engine = _make_engine()
        sent = []

        async def fake_ask_chatbot(**kwargs):
            sent.append([dict(message) for message in kwargs["messages"]])
            holder = kwargs.get("reasoning_holder")
            if holder is not None:
                holder["field"] = "reasoning_content"
                holder["text"] = "先想一想"
            return "日文原词\t中文翻译\t备注\nマジホ\t魔机\t物品", None

        engine.ask_chatbot = fake_ask_chatbot
        session = _ChatSession()
        await engine.llm_gen_dic("瑠那：マジホ", task_index=0, session=session)
        await engine.llm_gen_dic("瑠那：マジホ", task_index=1, session=session)
        # 第二轮发出去的历史里，第一轮的 assistant 消息带上了当初的思考内容
        assistant = [m for m in sent[1] if m["role"] == "assistant"][0]
        self.assertEqual(assistant["reasoning_content"], "先想一想")
        self.assertEqual(assistant["content"], "日文原词\t中文翻译\t备注\nマジホ\t魔机\t物品")
        # 思考内容每一轮都要重发，所以要算进会话字数
        self.assertGreater(session.chars, len("先想一想"))

    async def test_reasoning_field_is_stamped_on_every_assistant_turn(self):
        engine = _make_engine()
        replies = ["思考一", "", "思考三"]

        async def fake_ask_chatbot(**kwargs):
            holder = kwargs.get("reasoning_holder")
            if holder is not None:
                holder["field"] = "reasoning"
                holder["text"] = replies.pop(0)
            return "日文原词\t中文翻译\t备注\nマジホ\t魔机\t物品", None

        engine.ask_chatbot = fake_ask_chatbot
        session = _ChatSession()
        await engine.llm_gen_dic("瑠那：マジホ", task_index=0, session=session)
        await engine.llm_gen_dic("瑠那：マジホ", task_index=1, session=session)
        # 第二轮没有思考也要补空串：这场会话是 thinking 会话，历史里少一条就 400
        assistant_messages = [m for m in session.messages if m["role"] == "assistant"]
        self.assertEqual([m["reasoning"] for m in assistant_messages], ["思考一", ""])


class ProgressSpeedTests(unittest.TestCase):
    """「实时速度/预计剩余」必须与 x/130 项的进度同口径（见文件头第 7 条）。"""

    def _engine(self):
        engine = _make_engine()
        engine.progress_append_path = os.path.join(
            tempfile.mkdtemp(), "gendic_progress.append.jsonl"
        )
        engine.progress_started_at = time.monotonic()
        return engine

    def test_reports_items_per_minute_after_each_item(self):
        engine = self._engine()
        updates = []
        engine._update_runtime = lambda **kwargs: updates.append(kwargs)
        # 固定 monotonic 基准：直接写 monotonic() - 120 时，若机器开机不足 120 秒，
        # 结果会变成负数，撞上「未开始（<=0）」的保护值而得到 0.0（CI runner 偶发）
        base = 10_000.0
        with patch("GalTransl.Backend.GenDic.time.monotonic", return_value=base):
            engine.progress_started_at = base - 120  # 两分钟前开工
            for index in range(4):
                engine._append_runtime_progress(f"gendic-task-{index}", True)

            # 4 项 / 2 分钟 = 2 项/分，而不是「抽到了多少个术语/分」
            self.assertEqual(engine.progress_done, 4)
            self.assertTrue(all("progress_speed_lpm" in update for update in updates))
            self.assertAlmostEqual(updates[-1]["progress_speed_lpm"], 2.0, delta=0.3)

    def test_speed_is_zero_before_the_first_item(self):
        engine = self._engine()
        self.assertEqual(engine._progress_speed_lpm(), 0.0)  # 一项都没完成
        engine.progress_done = 5
        engine.progress_started_at = 0.0  # 没有开始时刻
        self.assertEqual(engine._progress_speed_lpm(), 0.0)

    def test_cleanup_clears_speed(self):
        engine = self._engine()
        updates = []
        engine._update_runtime = lambda **kwargs: updates.append(kwargs)
        engine.progress_done = 7

        engine._cleanup_runtime_progress()

        # 停止/跑完之后不能拿着最后一次的平均速度继续倒计时
        self.assertEqual(updates[-1]["progress_speed_lpm"], 0)
        self.assertEqual(engine.progress_done, 0)
        self.assertEqual(engine.progress_started_at, 0.0)
        self.assertEqual(engine.progress_append_path, "")


class ReasoningCaptureTests(unittest.TestCase):
    """思考内容的字段名因平台而异（与 Agent/core.py 同一套约定），直接属性和 model_extra 都要认。"""

    def test_reasoning_from_direct_attribute_and_model_extra(self):
        self.assertEqual(
            _extract_reasoning(SimpleNamespace(reasoning_content="先想想")),
            ("先想想", "reasoning_content"),
        )
        self.assertEqual(
            _extract_reasoning(SimpleNamespace(model_extra={"reasoning": "先想想"})),
            ("先想想", "reasoning"),
        )
        self.assertEqual(_extract_reasoning(SimpleNamespace(content="说")), ("", ""))
        self.assertEqual(_extract_reasoning(None), ("", ""))


class NameStepAndTriageTests(unittest.IsolatedAsyncioTestCase):
    """说话人名先统一译一遍（泛称也译），提取时不再每片重复输出；审校前分流，不沾边的地名/组织/种族直接保留。"""

    def test_name_batches_scale_with_workers_without_losing_names(self):
        names = [f"角色{i:03d}" for i in range(100)]
        self.assertEqual(len(_plan_name_batches(names, 1)), 2)
        batches = _plan_name_batches(names, 8)
        self.assertEqual(len(batches), 8)
        self.assertLessEqual(max(map(len, batches)), 13)
        self.assertEqual([name for batch in batches for name in batch], names)
        self.assertEqual(_plan_name_batches([], 8), [])
        self.assertEqual(_plan_name_batches(names[:5], 32), [names[:5]])
        self.assertEqual(_plan_name_batches(names, 0), _plan_name_batches(names, 1))
        many = names * 10
        self.assertLessEqual(max(map(len, _plan_name_batches(many, 8))), 60)

    async def test_name_pretranslation_uses_all_eight_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine()
            engine.pj_config.getProjectDir = lambda: tmp
            engine.wokers = 8
            updates = []
            engine._update_runtime = lambda **kwargs: updates.append(kwargs)
            engine._prepare_runtime_progress = lambda total: None
            engine._append_runtime_progress = lambda *args: None
            engine._load_existing_gpt_terms = lambda: {}
            names = [f"角色{i:02d}" for i in range(64)]
            engine._build_text_lines = lambda _: ([f"{n}：こんにちは" for n in names], names, set(names))
            active = peak = calls = 0
            all_started = asyncio.Event()

            async def fake_ask(prompt, label, index, session=None):
                nonlocal active, peak, calls
                if label == "人名批次":
                    calls += 1
                    active += 1
                    peak = max(peak, active)
                    if active == 8:
                        all_started.set()
                    try:
                        await asyncio.wait_for(all_started.wait(), timeout=1)
                        rows = prompt.split("## 名字\n", 1)[1].split("\n\n## 输出要求", 1)[0]
                        return "\n".join(f"{line.split(chr(9))[0]}\t译名\t人名" for line in rows.splitlines())
                    finally:
                        active -= 1
                if label == "分片":
                    return "NULL\tNULL\tNULL"
                rows = prompt.split("## 候选词条\n", 1)[1].split("\n\n## 输出要求", 1)[0]
                return "\n".join(f"{line.split(chr(9))[0]}\t译名\t人名" for line in rows.splitlines())

            engine._ask_gendic = fake_ask
            with open(os.path.join(tmp, "bccwj-suw+unidic_pos+pron.model"), "wb") as f:
                f.write(b"test model")
            with patch("tempfile.gettempdir", return_value=tmp), patch("vaporetto.Vaporetto", return_value=_FakeTokenizer({})):
                self.assertTrue(await engine.batch_translate([]))
            self.assertEqual(calls, 8)  # 固定 60 个/批时只有 2 个请求
            self.assertEqual(peak, 8)
            first_name_update = next(u for u in updates if u.get("stage") == "GenDic 人名翻译中")
            self.assertEqual(first_name_update["workers_active"], 8)
            saved = engine._load_existing_generated_terms(os.path.join(tmp, "项目GPT字典-生成.txt"))
            self.assertEqual(set(saved), set(names))

    async def test_all_speaker_names_are_translated_including_generic_ones(self):
        engine = _make_engine()
        prompts = []

        async def fake_ask(prompt, task_label, task_index):
            prompts.append(prompt)
            return (
                "日文原词\t中文翻译\t备注\n"
                "炎輝\t炎辉\t人名，男性，主角\n"
                "みんな\t大家\t称呼，说话人泛称\n"
                "$str20\t$str20\t人名，男性，主角名字的变量\n"
                "女の子Ａ\tDELETE\t\n"  # 模型没照做：不算结果，交回兜底规则
            )

        engine._ask_gendic = fake_ask
        text = "炎輝：「よし」\nみんな：「おー」\n$str20：「うん」\n女の子Ａ：「きゃ」"
        starts = [0]
        for line in text.split("\n")[:-1]:
            starts.append(starts[-1] + len(line) + 1)
        speakers = ["炎輝", "みんな", "$str20", "女の子Ａ"]
        ok = await engine._translate_names_batch(
            speakers, 0, {s: 1 for s in speakers}, text, starts, speakers, {}
        )
        self.assertTrue(ok)
        self.assertEqual(engine.name_decisions["みんな"], ("大家", "称呼，说话人泛称"))
        self.assertEqual(engine.name_decisions["$str20"][0], "$str20")
        self.assertNotIn("女の子Ａ", engine.name_decisions)
        self.assertIn("みんな\t1\tみんな：「おー」", prompts[0])

        final = engine._build_final_list([], set(speakers), {"炎輝", "$str20"}, text)
        self.assertEqual([row[0] for row in final][:3], ["炎輝", "みんな", "$str20"])

    async def test_translated_names_are_hinted_not_reextracted(self):
        engine = _make_engine()
        engine.existing_dict_map = {}
        engine.name_decisions = {"炎輝": ("炎辉", "人名，男性")}
        prompts = []

        async def fake_ask(prompt, task_label, task_index, session=None):
            prompts.append(prompt)
            return "日文原词\t中文翻译\t备注\n炎輝\t炎辉\t人名\nマジホ\t魔机\t物品"

        engine._ask_gendic = fake_ask
        await engine.llm_gen_dic("炎輝：マジホ", name_list=["炎輝"], task_index=0)
        self.assertIn("以下角色名已收录", prompts[0])
        self.assertIn("炎輝\t炎辉", prompts[0])
        self.assertNotIn("要加入术语表", prompts[0])
        # 模型仍输出了已译的名字：汇总时跳过，不当重复也不送审
        candidates, duplicates = engine._collect_candidates("炎輝：マジホ", set())
        self.assertEqual([c["src"] for c in candidates], ["マジホ"])
        self.assertEqual(duplicates, 0)

    def test_triage_keeps_unrelated_places_and_orgs(self):
        engine = _make_engine()
        for src, votes in {"ビキニ騎士団": 1, "和菓子開発部": 1, "ギルティア・ル・ドルード": 1, "ドルード": 1,
                           "逆バニー": 4, "猫ハゲ声": 1, "バニー学園": 5}.items():
            engine.dic_votes[src]["x"] = votes
        candidates = [
            _candidate("ビキニ騎士団", 3, "x", "组织，社团"),  # 不沾边的组织：直接保留
            _candidate("ギルティア・ル・ドルード", 1, "x", "人名"),  # 和已译人名相关：送审
            _candidate("ドルード", 3, "x", "组织，家族"),  # 是全名的一部分：送审统一译法
            _candidate("逆バニー", 10, "x", "物品，服装", votes=4),  # 多片一致：直接保留
            _candidate("猫ハゲ声", 1, "x", "其他，谐音梗"),  # 其他类：送审
            _candidate("バニー学園", 35, "x", "组织，学校", votes=5),  # 和逆バニー互不包含，不算沾边：直接保留
        ]
        direct, to_review = engine._triage_candidates(candidates, {"ギルティア"})
        self.assertEqual(sorted(c["src"] for c in direct), sorted(["ビキニ騎士団", "逆バニー", "バニー学園"]))
        self.assertEqual(
            sorted(c["src"] for c in to_review),
            sorted(["ギルティア・ル・ドルード", "ドルード", "猫ハゲ声"]),
        )


class CategorySectionTests(unittest.TestCase):
    """生成的字典按类别分区，分区之间用 ----------↓人名↓---------- 隔开。"""

    def test_category_from_note(self):
        cases = {
            "人名，女性": "人名",
            "姓氏，結灯的姓": "人名",
            "称呼，アリス对炎輝的称呼": "称呼",
            "昵称，ギルティア的昵称": "称呼",
            "地名": "地名",
            "社团/组织": "组织",
            "学校名，简称": "组织",
            "种族": "种族",
            "招式/技能": "技能",
            "物品，菜肴": "物品",
            "活动/比赛": "活动",
            "特殊用语": "其他",
            "": "其他",
        }
        for note, category in cases.items():
            self.assertEqual(_category_of(note), category, note)

    def test_new_file_is_grouped_in_category_order(self):
        lines = _merge_into_sections(
            [],
            [
                ["和菓子開発部", "和菓子开发部", "组织，社团"],
                ["瑠那", "瑠那", "人名，女性"],
                ["ギルちゃん", "小吉尔", "称呼，ギルティア的昵称"],
                ["炎輝", "炎辉", "人名，男性"],
            ],
        )
        self.assertEqual(
            lines,
            [
                "# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki",
                "",
                "----------↓人名↓----------",
                "瑠那\t瑠那\t人名，女性",
                "炎輝\t炎辉\t人名，男性",
                "",
                "----------↓称呼↓----------",
                "ギルちゃん\t小吉尔\t称呼，ギルティア的昵称",
                "",
                "----------↓组织↓----------",
                "和菓子開発部\t和菓子开发部\t组织，社团",
            ],
        )

    def test_rerun_appends_into_existing_sections_and_keeps_user_lines(self):
        existing = [
            "# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki",
            "手加的词\t手加\t",  # 旧格式/用户手加的、不在任何分区里的词条
            "",
            "----------↓人名↓----------",
            "瑠那\t瑠那\t人名，女性",
            "",
            "----------↓地名↓----------",
            "白鷺市\t白鹭市\t地名",
        ]
        lines = _merge_into_sections(
            existing,
            [["萌美奈", "萌美奈", "人名，女性"], ["マジホ", "魔机", "物品"]],
        )
        self.assertEqual(
            lines,
            [
                "# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki",
                "手加的词\t手加\t",
                "",
                "----------↓人名↓----------",
                "瑠那\t瑠那\t人名，女性",
                "萌美奈\t萌美奈\t人名，女性",
                "",
                "----------↓地名↓----------",
                "白鷺市\t白鹭市\t地名",
                "",
                "----------↓物品↓----------",
                "マジホ\t魔机\t物品",
            ],
        )

    def test_file_entry_count_matches_what_the_card_shows(self):
        """文件列表/标题里的「N 条」必须等于卡片里能看到的条目数。

        表头注释（`# 格式为日文[Tab]...`）以前被算成一条，于是出现「216 条有效条目」而卡片只有
        215 条（GPT 215 / 全部 215）这种对不上的情况。
        """
        import os
        import tempfile

        from GalTransl.server import _read_dict_file_payload

        lines = [
            "# 格式为日文[Tab]中文[Tab]解释(可不写)，参考项目wiki",
            "",
            "----------↓人名↓----------",
            "瑠那\t瑠那\t人名，女性",
            "萌美奈\t萌美奈\t人名，女性",
            "",
            "----------↓地名↓----------",
            "白鷺市\t白鹭市\t地名",
            "// 注释行\t注释",
            "没有 Tab 的行",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "项目GPT字典-生成.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.assertEqual(_read_dict_file_payload(path)["count"], 3)

    def test_section_lines_are_not_loaded_as_entries(self):
        import os
        import tempfile

        from GalTransl.Dictionary import CGptDict

        lines = _merge_into_sections([], [["瑠那", "瑠那", "人名，女性"], ["白鷺市", "白鹭市", "地名"]])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "项目GPT字典-生成.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            loaded = [d.search_word for d in CGptDict([path])._dic_list]
            self.assertEqual(loaded, ["瑠那", "白鷺市"])
            # 重跑时读已有词条去重，也不能把分区标题当成词
            self.assertEqual(set(_make_engine()._load_existing_generated_terms(path)), {"瑠那", "白鷺市"})


if __name__ == "__main__":
    unittest.main()
