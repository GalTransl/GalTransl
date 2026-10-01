"""patch_transl_cache 的换行归一化。

模型在 Markdown 表格里看到的换行是 <br>——这不是渲染器的发明：翻译管线送翻时，原文里的
换行本来就全部替换成 <br> 再发给模型（ForGalJsonTranslate.py:72-83），模型输出的 <br>
在正常翻译落盘前又会被换回真换行（_normalize_parsed_translation_text）。真正的缺口是
patch 的写路径没有这一步：模型照着表格写 <br> 就原样进了缓存。

归一化在 _plan_cache_patches 里做（真执行与审批卡预览共用同一份），把 <br> / 真换行 /
字面 \n 全部统一成**该条目自己的换行形式**；参考文本推断不出换行（整条没有换行）时原样
返回——管线在 n_symbol 为空时也不动 <br>，同一口径。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent.runtime import (
    _infer_linebreak_symbol,
    _normalize_linebreaks_like,
    _plan_cache_patches,
    _tool_patch_transl_cache,
)
from GalTransl.Agent.runtime import _PATCHABLE_FIELDS


def _entries():
    """三个条目三种换行风格：真 \r\n、字面 \n（JSON 内换行）、整条没有换行。"""
    return [
        {
            "index": 39,
            "post_src": "対対、円画…\r\n注意…",
            "pre_dst": "对对，绘制在圆圈那样\r\n不过，注意不要把热水直接淋在滤纸上",
        },
        # 字面 \n 风格的条目（JSON 内换行写成了字面 \n 两个字符）
        {"index": 40, "post_src": "セリフ\\n二行目", "pre_dst": "台词\\n第二行"},
        # 整条没有换行
        {"index": 41, "post_src": "单行原文", "pre_dst": "单行译文"},
    ]


class _StubRunner:
    """最小 runner：/cache/{file} 读条目，/cache/save 记录写回体。"""

    def __init__(self, entries):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self._entries = entries
        self.saved = None

    def _project_id(self):
        return "proj"

    def _http_get(self, url):
        return {"entries": [dict(e) for e in self._entries]}

    def _http_post(self, url, body):
        self.saved = [dict(e) for e in body["entries"]]
        return {"success": True, "entries": [dict(e) for e in body["entries"]]}


class InferLinebreakSymbolTests(unittest.TestCase):
    def test_priority_matches_the_pipeline(self):
        # 字面 \r\n > 真 \r\n > 字面 \n > 真 \n（ForGalJsonTranslate.py:72-79 同一顺序）
        self.assertEqual(_infer_linebreak_symbol("a\\r\\nb"), "\\r\\n")
        self.assertEqual(_infer_linebreak_symbol("a\r\nb"), "\r\n")
        self.assertEqual(_infer_linebreak_symbol("a\\nb"), "\\n")
        self.assertEqual(_infer_linebreak_symbol("a\nb"), "\n")
        self.assertEqual(_infer_linebreak_symbol("没有换行"), "")

    def test_first_reference_text_that_has_one_wins(self):
        # 字段现值优先于 post_src：正在编辑的那份用什么风格，就归一到什么风格
        self.assertEqual(_infer_linebreak_symbol("a\\nb", "a\r\nb"), "\\n")
        self.assertEqual(_infer_linebreak_symbol("", "a\r\nb"), "\r\n")
        self.assertEqual(_infer_linebreak_symbol(), "")


class NormalizeLinebreaksTests(unittest.TestCase):
    def test_br_becomes_the_real_newline_of_the_source(self):
        self.assertEqual(
            _normalize_linebreaks_like("对对，绘制在圆圈那样<br>不过", "原文第一行\n原文第二行"),
            "对对，绘制在圆圈那样\n不过",
        )

    def test_real_newline_becomes_literal_for_literal_style_entries(self):
        # JSON 内换行（字面 \n 两字符）的条目：真换行要写回成字面形式
        self.assertEqual(_normalize_linebreaks_like("A\nB", "src\\nsrc2"), "A\\nB")

    def test_doubly_escaped_newline_is_rescued(self):
        # 模型双重转义（写出了字面 \n 两个字符）而原文用真换行：也要救回来
        self.assertEqual(_normalize_linebreaks_like("A\\nB", "src\nsrc2"), "A\nB")

    def test_crlf_value_collapses_to_source_style(self):
        self.assertEqual(_normalize_linebreaks_like("A\r\nB", "x\ny"), "A\nB")
        self.assertEqual(_normalize_linebreaks_like("A\rB", "x\r\ny"), "A\r\nB")

    def test_br_variants_are_all_recognized(self):
        for token in ("<br>", "<BR>", "<Br>", "<br/>", "<br />"):
            self.assertEqual(_normalize_linebreaks_like(f"A{token}B", "x\ny"), "A\nB")

    def test_no_reference_linebreaks_leaves_value_alone(self):
        # 原文整条没有换行：管线在 n_symbol 为空时也不动 <br>（保持同一口径）
        self.assertEqual(_normalize_linebreaks_like("A<br>B", "没有换行的原文"), "A<br>B")

    def test_empty_value_returned_as_is(self):
        self.assertEqual(_normalize_linebreaks_like("", "x\ny"), "")


class PlanPatchNormalizationTests(unittest.TestCase):
    @staticmethod
    def _entries():
        return [
            {
                "index": 39,
                "post_src": "対対、円画…\r\n注意…",
                "pre_dst": "对对，绘制在圆圈那样\r\n不过，注意不要把热水直接淋在滤纸上",
            },
            # 字面 \n 风格的条目（JSON 内换行写成了字面 \n 两个字符）
            {"index": 40, "post_src": "セリフ\\n二行目", "pre_dst": "台词\\n第二行"},
            # 整条没有换行
            {"index": 41, "post_src": "单行原文", "pre_dst": "单行译文"},
        ]

    def test_br_written_by_the_model_becomes_the_entry_style(self):
        planned = _plan_cache_patches(
            self._entries(),
            [{"index": 39, "pre_dst": "对对，绘制在圆圈那样<br>不过，注意不要把热水直接淋在滤纸上"}],
            _PATCHABLE_FIELDS,
        )

        expected = "对对，绘制在圆圈那样\r\n不过，注意不要把热水直接淋在滤纸上"
        self.assertEqual(planned["plan"][0]["updates"]["pre_dst"], expected)
        # 变更卡上的 after 就是归一化后的值：所见即所得
        self.assertEqual(planned["changes"][0]["after"], expected)

    def test_real_newline_written_becomes_literal_for_literal_style_entries(self):
        # #40 是字面 \n 风格：模型写真换行（JSON 参数里就是 \n）→ 归一成字面 \n
        planned = _plan_cache_patches(
            self._entries(), [{"index": 40, "pre_dst": "台词\n第二行"}], _PATCHABLE_FIELDS
        )
        self.assertEqual(planned["plan"][0]["updates"]["pre_dst"], "台词\\n第二行")

    def test_proofread_comment_is_normalized_too(self):
        planned = _plan_cache_patches(
            self._entries(), [{"index": 40, "proofread_comment": "语气<br>和原文不对应"}], _PATCHABLE_FIELDS
        )
        self.assertEqual(planned["plan"][0]["updates"]["proofread_comment"], "语气\\n和原文不对应")

    def test_entry_without_linebreaks_is_left_alone(self):
        planned = _plan_cache_patches(
            self._entries(), [{"index": 41, "pre_dst": "改写<br>版"}], _PATCHABLE_FIELDS
        )
        self.assertEqual(planned["plan"][0]["updates"]["pre_dst"], "改写<br>版")

    def test_chained_patches_on_the_same_index_stay_normalized(self):
        # 同一 index 两条 patch：第二条的 before 要看见第一条（归一化后）的结果
        planned = _plan_cache_patches(
            self._entries(),
            [
                {"index": 39, "pre_dst": "第一版<br>两行"},
                {"index": 39, "pre_dst": "第二版<br>两行"},
            ],
            _PATCHABLE_FIELDS,
        )
        self.assertEqual(planned["changes"][1]["before"], "第一版\r\n两行")
        self.assertEqual(planned["changes"][1]["after"], "第二版\r\n两行")


class ToolEndToEndTests(unittest.TestCase):
    def test_tool_writes_the_normalized_value_to_the_cache(self):
        runner = _StubRunner(PlanPatchNormalizationTests._entries())

        result = _tool_patch_transl_cache(
            runner,
            {"filename": "a.json", "patches": [{"index": 39, "pre_dst": "对对，绘制在圆圈那样<br>不过，注意不要把热水直接淋在滤纸上"}]},
        )

        saved = {e["index"]: e for e in runner.saved}
        expected = "对对，绘制在圆圈那样\r\n不过，注意不要把热水直接淋在滤纸上"
        self.assertEqual(saved[39]["pre_dst"], expected)  # 落盘的是真 \r\n，不是 <br>
        self.assertEqual(result["changes"][0]["after"], expected)

    def test_values_without_linebreaks_pass_through_unchanged(self):
        runner = _StubRunner(PlanPatchNormalizationTests._entries())

        result = _tool_patch_transl_cache(
            runner, {"filename": "a.json", "patches": [{"index": 41, "pre_dst": "全新译文"}]}
        )

        self.assertEqual(result["changes"][0]["after"], "全新译文")

    def test_non_string_values_are_not_touched(self):
        # 白名单里没有数值字段，但归一化只该对字符串动手：用 dict 值走一遍也不会崩
        entries = [{"index": 41, "post_src": "单行原文", "pre_dst": "单行译文"}]
        planned = _plan_cache_patches(
            entries, [{"index": 41, "pre_dst": ["不是", "字符串"]}], _PATCHABLE_FIELDS
        )
        self.assertEqual(planned["plan"][0]["updates"]["pre_dst"], ["不是", "字符串"])


if __name__ == "__main__":
    unittest.main()
