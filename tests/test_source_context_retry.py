"""拆分重试：批次拆 1/3、前文拆 1/2；单句仍被拒时先把前文换成原文，再清空上文。"""

import unittest
from types import SimpleNamespace

from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
from GalTransl.Backend.ForGalMarkdownTranslate import ForGalMarkdownTranslate
from GalTransl.Backend.ForGalToolTranslate import ForGalToolTranslate
from GalTransl.Backend.ForNovelTranslate import ForNovelTranslate
from GalTransl.CSentense import CSentense


def _chain(texts: list[str], translations: list[str]) -> list[CSentense]:
    trans = []
    for i, (text, dst) in enumerate(zip(texts, translations)):
        tran = CSentense(text, "", i + 1)
        tran.post_src = text
        tran.pre_dst = dst
        if trans:
            tran.prev_tran = trans[-1]
            trans[-1].next_tran = tran
        trans.append(tran)
    return trans


def _translator(accept_when) -> tuple[ForGalJsonTranslate, list[str]]:
    """ForGalJson 实例：history_result 满足 accept_when 才给出译文，否则输出为空（被拦截）。"""
    translator = ForGalJsonTranslate.__new__(ForGalJsonTranslate)
    translator.pj_config = SimpleNamespace(active_workers=0, translation_guideline="", getProjectDir=lambda: "")
    translator.enhance_jailbreak = False
    translator.system_prompt = "system"
    translator.trans_prompt = "[Input]\n[Glossary]\n<history>[history_result]</history>"
    translator.contextNum = 8
    translator.last_translations = {}
    translator.target_lang = "English"
    translator.source_lang = "Japanese"
    translator.smartRetry = True
    translator.multi_turn = False
    translator.max_api_retries = 1
    translator._last_chatbot_was_stream = False
    translator._last_chatbot_model_name = ""
    translator._check_stop_requested = lambda: None
    translator._record_runtime_success = lambda filename, trans: None

    async def no_sleep(seconds):
        return None

    translator._interruptible_sleep = no_sleep
    translator._remember_translation_turn = lambda *args, **kwargs: None
    histories: list[str] = []

    def prepare(prompt_template, input_src, gptdict, trans_list, filename, proofread=False, assistant_prompt=""):
        prompt = translator._apply_history_result(prompt_template, filename)
        histories.append(prompt.split("<history>", 1)[1].split("</history>", 1)[0])
        return [], None, prompt

    translator._prepare_translation_messages = prepare

    async def ask(session, *, trans_list, sig_list, proofread, n_symbol="", **kwargs):
        if accept_when(histories[-1]):
            return f'{sig_list[0]}|{{"id": {trans_list[0].index}, "dst": "译文"}}', SimpleNamespace(model_name="m")
        return "", SimpleNamespace(model_name="m")

    translator._ask_translation_batch = ask
    return translator, histories


class SourceContextRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_context_comes_before_clearing_the_context(self) -> None:
        trans = _chain(["前一", "前二", "本句"], ["译一", "译二", ""])
        translator, histories = _translator(lambda history: history == "None")

        success_count, result = await translator.translate([trans[2]], filename="a.json")

        self.assertEqual((success_count, result[0].pre_dst), (1, "译文"))
        self.assertEqual(len(histories), 5)
        self.assertIn("译二", histories[0])  # 先按译文当前文
        self.assertIn('"src": "前二"', histories[3])  # 第 4 次：前文换成原文
        self.assertNotIn("译二", histories[3])
        self.assertEqual(histories[4], "None")  # 第 5 次：清空上文

    async def test_source_context_retry_can_succeed(self) -> None:
        trans = _chain(["前一", "前二", "本句"], ["译一", "译二", ""])
        translator, histories = _translator(lambda history: '"src": "前二"' in history)

        success_count, _ = await translator.translate([trans[2]], filename="a.json")

        self.assertEqual((success_count, len(histories)), (1, 4))

    async def test_split_retry_halves_the_context_with_each_split(self) -> None:
        """批次拆 1/3 时前文拆 1/2：9 句带 8 句前文 → 3 句带 4 句 → 1 句带 2 句。"""
        trans = _chain([f"前{i}" for i in range(8)] + [f"本{i}" for i in range(9)],
                       [f"译{i}" for i in range(8)] + [""] * 9)
        translator, histories = _translator(lambda history: False)
        translator.contextNum = 8

        await translator.translate(trans[8:], filename="a.json")

        counts = [history.count("old|") for history in histories]
        self.assertEqual(counts[:2], [8, 8])  # 9 句：拆分前的两次
        self.assertEqual(counts[2:4], [4, 4])  # 拆成 3 句
        self.assertEqual(counts[4:7], [2, 2, 2])  # 拆成 1 句
        self.assertEqual(histories[7].count("old|"), 2)  # 前文换成原文，也按拆分后的 2 句
        self.assertIn('"src": "前7"', histories[7])
        self.assertEqual(histories[8], "None")  # 最后清空上文
        self.assertEqual(len(histories), 9)

    async def test_still_fails_after_the_source_context_retry(self) -> None:
        trans = _chain(["前一", "本句"], ["译一", ""])
        translator, histories = _translator(lambda history: False)

        success_count, result = await translator.translate([trans[1]], filename="a.json")

        self.assertEqual(len(histories), 5)
        self.assertIn("(Failed)", result[0].pre_dst)

    async def test_no_preceding_lines_gives_up_as_before(self) -> None:
        trans = _chain(["本句"], [""])
        translator, histories = _translator(lambda history: False)

        await translator.translate(trans, filename="a.json")

        self.assertEqual(len(histories), 4)

    async def test_untranslated_preceding_lines_are_included_as_source(self) -> None:
        trans = _chain(["前一", "前二", "本句"], ["译一", "", ""])
        translator, _ = _translator(lambda history: False)
        self.assertTrue(translator.restore_source_context([trans[2]], 8, "a.json"))
        history = translator.last_translations["a.json"]
        self.assertIn('"src": "前一"', history)
        self.assertIn('"src": "前二"', history)


class SourceContextFormatTests(unittest.TestCase):
    def test_tables_label_the_column_as_source(self) -> None:
        trans = _chain(["前一", "本句"], ["译一", ""])
        for cls, marker in (
            (ForGalMarkdownTranslate, "| ID | NAME | SRC |"),
            (ForNovelTranslate, "SRC\tID"),
            (ForGalToolTranslate, "SRC"),
        ):
            translator = cls.__new__(cls)
            translator.last_translations = {}
            translator.include_speaker = True
            self.assertTrue(translator.restore_source_context([trans[1]], 8, "a"))
            history = translator.last_translations["a"]
            self.assertIn(marker, history, cls.__name__)
            self.assertIn("前一", history, cls.__name__)
            self.assertNotIn("译一", history, cls.__name__)


if __name__ == "__main__":
    unittest.main()
