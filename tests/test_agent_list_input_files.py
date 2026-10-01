"""list_input_files 报的句数一律取**输入文件解析出的条数**，且不带"这个数从哪来"的字段。

背景：为了估工作量，Agent 以前得逐个 read_input_file 去数句子；而且文件插件解析出的
是**原文条数**（文本插件如「跳过无日文句」还没跑），通常比真正要翻的句数大——于是出现
"估成 4000+，实际 2627"式的误判，直接影响 ETA。

后来有一版按"可靠性优先"分了两种来源：已有缓存的文件报缓存条数。那是个坑——缓存条数
只说明缓存里存了多少条，跟别处的数字一相等，读的人（Agent）就会以为整个文件翻完了；
而"条目数相等"恰恰是最容易被误读成"翻译完成"的形状。所以口径收成一个：

- 一律 → 原文解析条数（文本插件还没跑，估工作量偏大）；
- 解析失败 → null；
- 进度不在这里看（get_project_overview 的 files_translated/files_total 才算）。

顺带锁住 sentences_source 这个字段被删干净——留着它，模型就会去比较两种来源。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl.Agent.runtime import AgentRunner, AgentState, _tool_list_input_files


class _Runner:
    """最小 runner：/files?counts=1 返回输入文件（附解析条数）。"""

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
    def test_cached_file_still_reports_the_parsed_count(self) -> None:
        """有缓存的文件也报原文解析条数——缓存条数是"存了多少条"，不是"翻了多少"。"""
        runner = _Runner(
            _payload(
                [_infile("sc_2_st10.json", sentences=130)],
                [_cache("sc_2_st10.json", 120)],  # 缓存里有 120 条：不参与句数
            )
        )
        out = _tool_list_input_files(runner, {})
        item = out["input_files"][0]
        self.assertEqual(item["sentences"], 130)
        self.assertNotIn("sentences_source", item)
        self.assertEqual(item["size"], 100)  # 大小仍然保留（挑小文件试译用）

    def test_cache_chunking_never_leaks_into_the_count(self) -> None:
        """不管缓存怎么分块、有没有 .append，句数都只认输入文件。"""
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
        self.assertEqual(item["sentences"], 999)
        self.assertNotIn("sentences_source", item)

    def test_nested_input_file_is_counted_from_the_file_itself(self) -> None:
        """输入文件带目录（缓存名会把分隔符换成 -}）也只数原文件。"""
        runner = _Runner(
            _payload(
                [_infile("chapter/scene.json", sentences=10)],
                [_cache("chapter-}scene.json", 42)],
            )
        )
        item = _tool_list_input_files(runner, {})["input_files"][0]
        self.assertEqual(item["sentences"], 10)

    def test_parse_failure_is_null_not_zero(self) -> None:
        """解析失败给 null：0 会被读成"这个文件不用翻"。"""
        runner = _Runner(
            _payload(
                [
                    _infile("a.json", sentences=145),
                    _infile("b.json", sentences=None),
                ],
                [],
            )
        )
        out = _tool_list_input_files(runner, {})
        first, second = out["input_files"]
        self.assertEqual(first["sentences"], 145)
        self.assertIsNone(second["sentences"])
        self.assertNotIn("sentences_source", first)

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

    def test_total_and_note_are_all_from_the_input_files(self) -> None:
        runner = _Runner(
            _payload(
                [
                    _infile("cached.json", sentences=130),
                    _infile("fresh.json", sentences=145),
                    _infile("broken.json", sentences=None),
                ],
                [_cache("cached.json", 120)],  # 有缓存也不改变总数
            )
        )
        out = _tool_list_input_files(runner, {})

        self.assertEqual(out["sentences_total"], 130 + 145)  # 只累加已知的
        note = out["note"]
        self.assertIn("偏大", note)  # 明确提示这是解析条数、估工作量偏大
        self.assertIn("null", note)
        self.assertIn("不是进度", note)  # 明确否掉"拿它当进度"的用法
        self.assertNotIn("sentences_source", note)  # 字段既然删了，说明里也不该再提

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
