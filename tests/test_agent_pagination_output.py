"""分页输出保留正文和定位信息，计数、行号及续页提示不重复展开。"""

import copy

import pytest

from GalTransl.Agent.tools.cache import _tool_list_transl_cache
from GalTransl.Agent.tools.input import _tool_list_input_files
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from GalTransl.Agent.tools.search import _tool_search_input
from tests.test_agent_list_tools import _ListRunner
from tests.test_agent_read_dict import _read
from tests.test_agent_search_input import _SearchRunner


@pytest.mark.parametrize("offset, positions", [(0, "1-200"), (200, "201-317")])
def test_dictionary_page_compacts_positions_without_losing_text(offset, positions):
    lines = [f"词{i}\t译名{i}\t注释" for i in range(317)]
    result = _read(lines, offset=offset, limit=200)
    original = copy.deepcopy(result)
    text = _render_tool_result_table("read_dict", result)
    assert f"原文件行号：{positions}" in text
    assert "1, 2, 3" not in text
    assert "匹配 317 行" not in text
    assert "\n".join(result["lines"]) in text
    assert result == original
    if offset == 0:
        assert text.count("offset=200") == 1
        assert "offset=0" not in text
        return
    assert "还有更多" not in text


def test_dictionary_search_keeps_sparse_positions_in_the_original_order():
    lines = ["a\tA", "a\tB", "b\tB", "c\tC", "a\tE", "f\tF", "g\tG", "a\tH", "a\tI", "a\tJ"]
    result = _read(lines, query="a")
    text = _render_tool_result_table("read_dict", result)
    assert "原文件行号：1-2, 5, 8-10" in text
    assert result["line_numbers"] == [1, 2, 5, 8, 9, 10]
    assert "\n".join(result["lines"]) in text


@pytest.mark.parametrize("read, name, unit", [
    (_tool_list_transl_cache, "read_transl_cache", "缓存文件"),
    (_tool_list_input_files, "list_input_files", "输入文件"),
])
@pytest.mark.parametrize("order", ["name", "even", "random"])
def test_file_list_counts_and_paging_are_shown_once(read, name, unit, order):
    files = [{"name": f"f{i:03}.json", "size": i, "sentences": 1} for i in range(317)]
    result = read(_ListRunner(cache_files=files, input_files=files), {"order": order, "limit": 200})
    original = copy.deepcopy(result)
    text = _render_tool_result_table(name, result)
    assert text.count(f"共 317 个{unit}") == 1
    assert text.count(f"本页 200 个{unit}") == 1
    assert "offset=0" not in text
    assert result == original
    if order == "name":
        assert text.count("offset=200") == 1
        assert text.count("文件名顺序") == 1
        return
    assert "offset=200" not in text
    assert text.count("随机采样" if order == "random" else "均匀采样") == 1
    assert "完整遍历" in text


def test_missing_indexes_are_bounded_and_compact():
    result = {"filename": "a.json", "count": 1, "returned": 1, "entries": [{"index": 300, "pre_src": "正文"}],
              "missing_indexes": list(range(1, 201)), "missing_count": 999999999, "missing_truncated": True}
    text = _render_tool_result_table("read_input_file", result)
    assert "缺失 index：1-200" in text
    assert "缺失 index 共 999999999 个" in text
    assert len(text) < 500


def test_name_page_does_not_list_missing_names_twice():
    result = {"total": 400, "matched": 40, "returned": 2, "offset": 2, "has_more": True, "next_offset": 4,
              "names": [{"src_name": "独特甲", "dst_name": "", "count": 3},
                        {"src_name": "独特乙", "dst_name": "乙", "count": 1}], "still_empty": ["独特甲"]}
    text = _render_tool_result_table("get_name_table", result)
    assert text.count("独特甲") == 1
    assert text.count("筛选后 40 条人名") == 1
    assert "本页 2 条人名" in text
    assert text.count("offset=4") == 1


@pytest.mark.parametrize("order", ["name", "even", "random"])
def test_search_continuation_counts_hits_and_diagnostics_are_not_repeated(order):
    runner = _SearchRunner(response={"total": 100, "returned_hits": 1, "returned": 2,
                                    "files_failed": ["broken.json"], "results": [
        {"filename": "a.json", "index": 1, "src": "上文"},
        {"filename": "a.json", "index": 2, "src": "match", "match_src": True},
    ]})
    result = _tool_search_input(runner, {"query": "match", "context": 1, "offset": 40, "order": order})
    text = _render_tool_result_table("search_input", result)
    assert text.count("broken.json") == 1
    assert text.count("index 带 *") == 1
    assert "未知" in text
    assert result["returned"] == 1
    if order == "name":
        assert result["next_offset"] == 41
        assert "offset=41" in text
        return
    assert "next_offset" not in result
    assert "offset=41" not in text
