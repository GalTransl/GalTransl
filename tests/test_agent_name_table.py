"""get_name_table 与 GPT 字典（dictionary.useGPTDictInName）。

回归背景：翻译时 name 字段会吃 GPT 字典里同名的词条（GalTransl/Name.py 的 load_name_table），
而人名表里"译名空着、字典里有"的行其实早已生效。工具以前只把表原样丢给模型，模型就会去补
一整批"字典里早就写过"的名字。现在按同一口径把字典译名补进返回值，并把"真正还缺的"单列成
still_empty——这份测试钉的就是"补哪些、不补哪些"和"绝不写盘"。
"""

import unittest
from types import SimpleNamespace

from GalTransl.Agent import runtime as rt
from GalTransl.Agent.runtime import _tool_get_name_table


class _NameRunner:
    """只读替身：备好人名表 / 项目配置 / GPT 字典，并记录写操作与各地址被读了几次。"""

    def __init__(
        self,
        *,
        names=None,
        config=None,
        gpt_dict_files=None,
        dict_contents=None,
        config_error=False,
        dict_error=False,
    ):
        self.state = SimpleNamespace(config_file_name="config.yaml", project_dir=r"C:\proj")
        self.names = names if names is not None else []
        self.config = config
        self.gpt_dict_files = gpt_dict_files if gpt_dict_files is not None else []
        self.dict_contents = dict_contents if dict_contents is not None else {}
        self.config_error = config_error
        self.dict_error = dict_error
        self.config_reads = 0
        self.writes: list[str] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, url: str):
        if url.endswith("/name-table"):
            return {"source_file": "name替换表.csv", "names": [dict(n) for n in self.names]}
        if "/dictionary/project" in url:
            if self.dict_error:
                raise RuntimeError("字典读不到")
            return {"gpt_dict_files": list(self.gpt_dict_files), "dict_contents": dict(self.dict_contents)}
        if "/config?" in url:
            self.config_reads += 1
            if self.config_error:
                raise RuntimeError("配置读不到")
            return {"config": self.config}
        raise AssertionError(f"不该读这个地址：{url}")

    def _http_post(self, url: str, _body):
        self.writes.append(url)
        return {}

    def _http_put(self, url: str, _body):
        self.writes.append(url)
        return {}


def _dict_file(*lines: str) -> dict:
    return {"gpt_dict.txt": {"lines": list(lines), "count": len(lines)}}


def _config(enabled) -> dict:
    """项目配置（字典段）。enabled=None 表示配置里没有这个键。"""
    dictionary = {} if enabled is None else {"useGPTDictInName": enabled}
    return {"dictionary": dictionary, "common": {}}


class GptDictParityTests(unittest.TestCase):
    """口径一致要拿真身对答案：直接和翻译侧的 CGptDict.get_dst 比。"""

    _LINES = [
        "ドルード\t多鲁德",
        "アリス    爱丽丝",  # 4 个空格当 Tab
        "ボブ->鲍勃 #昵称",  # src->dst #note
        "// 注释行",
        "壊れた行",
        "",
        "ドルード\t后来的写法",  # 同一查找词：首个命中者胜
    ]

    def test_lookup_agrees_with_cgptdict(self):
        import os
        import tempfile

        from GalTransl.Dictionary import CGptDict

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gpt_dict.txt")
            with open(path, "w", encoding="utf8") as f:
                f.write("\n".join(self._LINES) + "\n")
            real = CGptDict([path])

        ours: dict[str, str] = {}
        for line in self._LINES:
            src, dst = rt._gpt_dict_line(line)
            if src and dst:
                ours.setdefault(src, dst)

        for word in ("ドルード", "アリス", "ボブ", "壊れた行", "誰でもない"):
            self.assertEqual(ours.get(word, ""), real.get_dst(word), word)


class NameTableGptDictTests(unittest.TestCase):
    """开着 useGPTDictInName 时：补空译名、保留已有译名、点出真正还缺的。"""

    def test_empty_dst_names_are_filled_from_gpt_dict(self):
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": "", "count": 12}],
            config=_config(True),
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード\t多鲁德"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["names"][0]["dst_name"], "多鲁德")
        self.assertEqual(result["names"][0]["dst_name_source"], "gpt_dict")
        self.assertEqual(result["filled_from_gpt_dict"], ["ドルード"])
        self.assertEqual(result["still_empty"], [])
        self.assertTrue(result["use_gpt_dict_in_name"])
        self.assertEqual(runner.writes, [])  # 只读：一个字节都不写

    def test_existing_dst_name_is_never_overridden_by_the_dict(self):
        """表里写下的译名是决定，字典只补空的——与前端人名页同一套规则。"""
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": "杜鲁德", "count": 3}],
            config=_config(True),
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード\t多鲁德"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["names"][0]["dst_name"], "杜鲁德")
        self.assertNotIn("dst_name_source", result["names"][0])
        self.assertEqual(result["filled_from_gpt_dict"], [])

    def test_names_missing_everywhere_land_in_still_empty(self):
        runner = _NameRunner(
            names=[
                {"src_name": "アリス", "dst_name": "", "count": 5},
                {"src_name": "ドルード", "dst_name": "", "count": 2},
                {"src_name": "ボブ", "dst_name": "鲍勃", "count": 1},
            ],
            config=_config(True),
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード\t多鲁德"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["filled_from_gpt_dict"], ["ドルード"])
        self.assertEqual(result["still_empty"], ["アリス"])  # 只有它要动手补
        self.assertEqual(
            [n["dst_name"] for n in result["names"]], ["", "多鲁德", "鲍勃"]
        )

    def test_dict_entries_from_all_configured_gpt_files_are_merged(self):
        runner = _NameRunner(
            names=[{"src_name": "アリス", "dst_name": ""}, {"src_name": "ボブ", "dst_name": ""}],
            config=_config(True),
            gpt_dict_files=["a.txt", "b.txt"],
            dict_contents={
                "a.txt": {"lines": ["アリス\t爱丽丝"], "count": 1},
                "b.txt": {"lines": ["ボブ\t鲍勃"], "count": 1},
            },
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(
            [n["dst_name"] for n in result["names"]], ["爱丽丝", "鲍勃"]
        )


class GptDictParsingTests(unittest.TestCase):
    """字典行的解析口径照抄 CGptDict.load_dic / get_dst（命中是全等比较）。"""

    def test_line_forms_follow_the_translation_side(self):
        cases = {
            "ドルード\t多鲁德": ("ドルード", "多鲁德"),
            "ドルード    多鲁德": ("ドルード", "多鲁德"),  # 4 个空格当 Tab
            # src->dst #note：# 也并成 Tab，于是替换词末尾留了个空格——CGptDict 就是这么读的
            # （它不 trim），翻译时写进 name 字段的也确实是带空格的那份
            "ドルード->多鲁德 #人名": ("ドルード", "多鲁德 "),
            "ドルード": ("", ""),  # 只有一列，不成词条
            "": ("", ""),
            "\n": ("", ""),
            "// 这行是注释\t注释": ("", ""),
            "\\\\ 注释\t注释": ("", ""),
        }
        for raw, expected in cases.items():
            self.assertEqual(rt._gpt_dict_line(raw), expected, raw)

    def test_first_occurrence_wins_for_a_repeated_search_word(self):
        """CGptDict.get_dst 取第一个命中的词条：后写的不会覆盖先写的。"""
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}],
            config=_config(True),
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード\t先写的", "ドルード\t后写的"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["names"][0]["dst_name"], "先写的")

    def test_lookup_is_exact_so_lookalikes_are_not_covered(self):
        """空白不做 strip：字典里带空格的那行在翻译时也查不到，别在这里假装命中。"""
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}],
            config=_config(True),
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード \t多鲁德"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["names"][0]["dst_name"], "")
        self.assertEqual(result["still_empty"], ["ドルード"])


class GptDictToggleTests(unittest.TestCase):
    """开关没开（或缺键）就别补：翻译时不会生效的东西不能展示成"已有人管"。"""

    def test_disabled_flag_returns_the_raw_table_and_never_reads_the_dict(self):
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}],
            config=_config(False),
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード\t多鲁德"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(list(result.keys()), ["source_file", "names"])
        self.assertEqual(result["names"][0]["dst_name"], "")
        self.assertEqual(runner.writes, [])

    def test_missing_flag_counts_as_off(self):
        """与 Name.py 的取值口径一致：配置里没写 = 翻译时不吃字典。"""
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}],
            config=_config(None),
        )

        result = _tool_get_name_table(runner, {})

        self.assertNotIn("filled_from_gpt_dict", result)

    def test_flat_and_expanded_config_key_both_work(self):
        """配置里既有 useGPTDictInName，也可能写成 dictionary.useGPTDictInName。"""
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}],
            config={"dictionary": {"dictionary.useGPTDictInName": True}},
            gpt_dict_files=["gpt_dict.txt"],
            dict_contents=_dict_file("ドルード\t多鲁德"),
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["names"][0]["dst_name"], "多鲁德")


class NameTableDegradationTests(unittest.TestCase):
    """读不到配置/字典、或表为空时：退回原样返回，绝不把工具弄失败。"""

    def test_config_read_failure_degrades_to_the_raw_table(self):
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}], config_error=True
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(list(result.keys()), ["source_file", "names"])

    def test_dict_read_failure_degrades_to_the_raw_table(self):
        runner = _NameRunner(
            names=[{"src_name": "ドルード", "dst_name": ""}],
            config=_config(True),
            dict_error=True,
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(list(result.keys()), ["source_file", "names"])

    def test_empty_name_table_skips_the_config_lookup_entirely(self):
        runner = _NameRunner(names=[], config=_config(True))

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["names"], [])
        self.assertEqual(runner.config_reads, 0)  # 表都是空的，没必要再读配置

    def test_empty_gpt_dict_reports_everything_as_still_empty(self):
        runner = _NameRunner(
            names=[{"src_name": "アリス", "dst_name": ""}], config=_config(True)
        )

        result = _tool_get_name_table(runner, {})

        self.assertEqual(result["filled_from_gpt_dict"], [])
        self.assertEqual(result["still_empty"], ["アリス"])


if __name__ == "__main__":
    unittest.main()
