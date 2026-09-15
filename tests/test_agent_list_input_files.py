"""list_input_files 要给每个文件的待翻译句数，并且标明这个数是怎么来的。

背景：为了估工作量，Agent 以前得逐个 read_input_file 去数句子；而且文件插件解析出的
是**原文条数**（文本插件如「跳过无日文句」还没跑），通常比真正要翻的句数大——于是出现
"估成 4000+，实际 2627"式的误判，直接影响 ETA。

现在的口径（按可靠性优先）：

- 已有缓存的文件 → 缓存条数（sentences_source=cache，与进度/ETA 同口径，准确）；
- 尚未翻译的文件 → 原文解析条数（=input，估计值，可能偏大）；
- 解析失败 → null。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl.Agent.runtime import AgentRunner, AgentState, _tool_list_input_files


class _Runner:
    """最小 runner：/files?counts=1 返回输入文件与缓存条数。"""

    def __init__(self, payload: dict) -> None:
        self.state = AgentState(config_file_name="config.yaml")
        self.payload = payload
        self.paths: list[str] = []

    def _project_id(self) -> str:
        return "proj"

    def _http_get(self, path: str):
        self.paths.append(path)
        assert "/files?" in path, f"list_input_files 应带参数请求 /files：{path}"
        assert "counts=1" in path, f"要句数就得带 counts=1：{path}"
        assert "config=config.yaml" in path, f"解析原文要带 config：{path}"
        return self.payload


def _payload(input_files, cache_files):
    return {"input_files": input_files, "cache_files": cache_files}


def _infile(name: str, sentences, size: int = 100) -> dict:
    return {"name": name, "is_file": True, "size": size, "sentences": sentences}


def _cache(name: str, entry_count: int) -> dict:
    return {"name": name, "is_file": True, "size": 10, "entry_count": entry_count}


class ListInputFilesSentencesTests(unittest.TestCase):
    def test_cached_file_uses_cache_count_not_parsed(self) -> None:
        """已有缓存 → 用缓存条数（准确），压掉偏大的原文条数。"""
        runner = _Runner(
            _payload(
                [_infile("sc_2_st10.json", sentences=130)],
                [_cache("sc_2_st10.json", 120)],
            )
        )
        out = _tool_list_input_files(runner, {})
        item = out["input_files"][0]
        self.assertEqual(item["sentences"], 120)
        self.assertEqual(item["sentences_source"], "cache")
        self.assertEqual(item["size"], 100)  # 大小仍然保留（挑小文件试译用）

    def test_multi_chunk_cache_is_summed(self) -> None:
        """多分块缓存的句数要相加（与进度统计同一个归属规则）。"""
        runner = _Runner(
            _payload(
                [_infile("sc_2_st11.json", sentences=999)],
                [
                    _cache("sc_2_st11.json_0.json", 30),
                    _cache("sc_2_st11.json_1.json", 25),
                    _cache("sc_2_st11.json.append.jsonl", 0),
                ],
            )
        )
        item = _tool_list_input_files(runner, {})["input_files"][0]
        self.assertEqual(item["sentences"], 55)
        self.assertEqual(item["sentences_source"], "cache")

    def test_path_separator_naming_is_matched(self) -> None:
        """输入文件带目录时，缓存名会把分隔符换成 -}。"""
        runner = _Runner(
            _payload(
                [_infile("chapter/scene.json", sentences=10)],
                [_cache("chapter-}scene.json", 42)],
            )
        )
        item = _tool_list_input_files(runner, {})["input_files"][0]
        self.assertEqual(item["sentences"], 42)
        self.assertEqual(item["sentences_source"], "cache")

    def test_uncached_file_falls_back_to_parsed_count(self) -> None:
        runner = _Runner(
            _payload(
                [
                    _infile("a.json", sentences=145),  # 没缓存 → 用解析条数（估计）
                    _infile("b.json", sentences=None),  # 解析失败 → null
                ],
                [],
            )
        )
        out = _tool_list_input_files(runner, {})
        first, second = out["input_files"]
        self.assertEqual((first["sentences"], first["sentences_source"]), (145, "input"))
        self.assertEqual((second["sentences"], second["sentences_source"]), (None, ""))

    def test_directories_are_skipped(self) -> None:
        runner = _Runner(
            _payload(
                [
                    {"name": "subdir", "is_file": False, "size": 0, "sentences": None},
                    _infile("a.json", sentences=7),
                ],
                [],
            )
        )
        out = _tool_list_input_files(runner, {})
        self.assertEqual([f["name"] for f in out["input_files"]], ["a.json"])
        self.assertEqual(out["count"], 1)

    def test_total_and_note_explain_the_two_scales(self) -> None:
        runner = _Runner(
            _payload(
                [
                    _infile("cached.json", sentences=130),
                    _infile("fresh.json", sentences=145),
                    _infile("broken.json", sentences=None),
                ],
                [_cache("cached.json", 120)],
            )
        )
        out = _tool_list_input_files(runner, {})

        self.assertEqual(out["sentences_total"], 120 + 145)  # 只累加已知的
        note = out["note"]
        self.assertIn("sentences_source=cache", note)
        self.assertIn("=input", note)
        self.assertIn("null", note)
        self.assertIn("偏大", note)  # 明确提示 input 是估计值
        self.assertIn("1 个文件是准确值，1 个是估计值", note)

    def test_no_total_when_nothing_known(self) -> None:
        runner = _Runner(_payload([_infile("a.json", sentences=None)], []))
        self.assertEqual(_tool_list_input_files(runner, {})["sentences_total"], 0)


class RealRunnerRequestTests(unittest.TestCase):
    """用真 AgentRunner 走一遍 URL 拼装（含 config 名里的特殊字符）。"""

    def test_config_name_is_url_encoded(self) -> None:
        state = AgentState(config_file_name="config v2.yaml")
        state.project_dir = os.path.join(tempfile.mkdtemp(prefix="agent-list-input-"), "MyGame")
        runner = AgentRunner(state)
        captured: list[str] = []

        def fake_get(_self, path: str):
            captured.append(path)
            return {"input_files": [], "cache_files": []}

        with patch.object(AgentRunner, "_http_get", fake_get):
            _tool_list_input_files(runner, {})

        self.assertEqual(len(captured), 1)
        self.assertIn("config=config%20v2.yaml", captured[0])


if __name__ == "__main__":
    unittest.main()
