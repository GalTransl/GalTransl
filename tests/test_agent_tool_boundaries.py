import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from GalTransl.Agent.tools.cache import _read_transl_cache_entries, _tool_read_output, _tool_list_transl_cache, _tool_patch_transl_cache
from GalTransl.Agent.tools.input import _tool_read_input_file, _tool_list_input_files
from GalTransl.Agent.tools.dicts import _dict_new_lines
from GalTransl.Agent.tools.names import _tool_get_name_table, _tool_save_name_table
from GalTransl.Agent.tools.preview import _preview_cache_patch, _preview_dict_write
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Dictionary import CGptDict, CNormalDic
from tests.test_agent_save_dict import _run, _Runner as DictRunner
from tests.test_agent_replace_cache import _Runner as CacheRunner
from tests.test_agent_save_name_table import _Runner as NamesRunner
from tests.test_agent_name_table import _NameRunner, _config, _dict_file


class DictionaryRuleTests(unittest.TestCase):
    def test_arrow_entry_is_updated_and_translation_loader_uses_new_value(self):
        lines, stats = _dict_new_lines(["Alice->Old#note", "Bob\tB"], "Alice\tNew\tnew note", "patch", category="gpt")
        self.assertEqual(lines, ["Alice\tNew\tnew note", "Bob\tB"])
        self.assertEqual(stats["replaced_keys"], ["Alice"])
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "dict.txt"
            file.write_text("\n".join(lines), encoding="utf-8")
            self.assertEqual(CGptDict([str(file)]).get_dst("Alice"), "New")

    def test_same_word_in_different_conditions_and_scenes_is_preserved(self):
        old = ["pre_src\tA\tword\tA", "pre_src\tB\tword\tB", "post_src\tB\tword\tPost",
               "mono\tword\tMono", "diag\tword\tDialog", "word\tPlain", "^^word\tStart"]
        lines, _ = _dict_new_lines(old, "pre_src\tB\tword\tNewB\ndiag\tword\tNewDialog", "patch", category="post")
        expected = list(old)
        expected[1] = "pre_src\tB\tword\tNewB"
        expected[4] = "diag\tword\tNewDialog"
        self.assertEqual(lines, expected)
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "post.txt"
            file.write_text("\n".join(lines), encoding="utf-8")
            dictionary = CNormalDic([str(file)])
            self.assertEqual([row.replace_word for row in dictionary.dic_list], ["A", "NewB", "Post", "Mono", "NewDialog", "Plain", "Start"])
        remaining, _ = _dict_new_lines(lines, "pre_src\tB\tword", "delete", category="post")
        self.assertEqual(remaining, expected[:1] + expected[2:])

    def test_gpt_reserved_words_and_normal_literal_arrows_are_not_misparsed(self):
        lines, _ = _dict_new_lines(["mono\told\tnote"], "mono\tnew\tnew note", "patch", category="gpt")
        self.assertEqual(lines, ["mono\tnew\tnew note"])
        lines, _ = _dict_new_lines(["a->b\told", "a\tkeep"], "a->b\tnew", "patch", category="post")
        self.assertEqual(lines, ["a->b\tnew", "a\tkeep"])

    def test_key_whitespace_and_escaped_newlines_follow_loader_rules(self):
        lines, _ = _dict_new_lines([" a \tOld", "a\tKeep"], " a \tNew", "patch", category="gpt")
        self.assertEqual(lines, [" a \tNew", "a\tKeep"])
        lines, _ = _dict_new_lines([r"a\nb" + "\tOld"], r"a\nb" + "\tNew", "patch", category="post")
        self.assertEqual(lines, [r"a\nb" + "\tNew"])

    def test_default_saves_preserve_unmentioned_dictionary_and_name_entries(self):
        result, runner = _run(["a\tA", "b\tB"], "a\tNewA")
        self.assertEqual(result["action"], "patch")
        self.assertEqual(runner.saved, ["a\tNewA\nb\tB"])
        names = NamesRunner([{"src_name": "A", "dst_name": "Alpha", "count": 9}])
        result = _tool_save_name_table(names, {"names": [{"src_name": "B", "dst_name": "Beta"}]})
        self.assertEqual(result["mode"], "patch")
        self.assertEqual([row["src_name"] for row in names.saved[-1]], ["A", "B"])
        runner = DictRunner(["Alice->Old#note"])
        preview = _preview_dict_write(runner, {"file_key": "(project_dir)项目GPT字典.txt", "content": "Alice\tNew"})
        self.assertEqual(preview["action"], "patch")
        self.assertEqual(preview["line_diff"]["removed"], 1)


class CacheNoopTests(unittest.TestCase):
    def test_identical_and_normalized_values_do_not_save_or_change_provenance(self):
        for before, incoming in (("same", "same"), (r"line\nbreak", "line\nbreak")):
            with self.subTest(before=before):
                runner = CacheRunner({"a.json": [{"index": 1, "pre_dst": before, "trans_by": "original"}]})
                args = {"filename": "a.json", "patches": [{"index": 1, "pre_dst": incoming}]}
                self.assertIsNone(_preview_cache_patch(runner, args))
                result = _tool_patch_transl_cache(runner, args)
                self.assertEqual(result["updated"], 0)
                self.assertEqual(runner.saved, [])
                self.assertEqual(runner.disk["a.json"][0]["trans_by"], "original")

    def test_comment_only_change_keeps_translator_even_with_identical_translation(self):
        runner = CacheRunner({"a.json": [{"index": 1, "pre_dst": "same", "proofread_comment": "fix", "trans_by": "original"}]})
        result = _tool_patch_transl_cache(runner, {"filename": "a.json", "clear_comment": True,
                                                 "patches": [{"index": 1, "pre_dst": "same"}]})
        self.assertEqual([row["path"] for row in result["changes"]], ["#1.proofread_comment"])
        self.assertEqual(runner.disk["a.json"][0]["trans_by"], "original")


class ReadPagingTests(unittest.TestCase):
    def runner(self, entries):
        return SimpleNamespace(state=SimpleNamespace(config_file_name="config.yaml"),
                               _project_id=lambda: "proj", _http_get=lambda url: {"entries": entries})

    def test_explicit_ranges_page_without_duplicates_for_all_read_tools(self):
        rows = [{"index": i, "pre_src": str(i), "pre_dst": str(i)} for i in range(1, 501)]
        for tool in (_tool_read_input_file, _tool_read_output, _read_transl_cache_entries):
            with self.subTest(tool=tool.__name__):
                seen, offset = [], 0
                while True:
                    page = tool(self.runner(rows), {"filename": "a.json", "index": "1-500", "limit": 80, "offset": offset})
                    self.assertLessEqual(page["returned"], 80)
                    seen.extend(row["index"] for row in page["entries"])
                    if not page["has_more"]:
                        break
                    offset = page["next_offset"]
                self.assertEqual(seen, list(range(1, 501)))
                last = tool(self.runner(rows), {"filename": "a.json", "index": "1-500", "offset": 999})
                self.assertEqual(last["entries"], [])
                self.assertFalse(last["has_more"])

    def test_default_and_extreme_index_ranges_stay_bounded_including_missing_ids(self):
        rows = [{"index": i, "pre_src": str(i)} for i in range(1, 501)]
        for tool in (_tool_read_input_file, _tool_read_output, _read_transl_cache_entries):
            with self.subTest(tool=tool.__name__):
                result = tool(self.runner(rows), {"filename": "a.json", "index": "1-1000000000"})
                self.assertEqual(result["returned"], 200)
                self.assertEqual(result["missing_count"], 1000000000 - 500)
                self.assertEqual(len(result["missing_indexes"]), 200)
                self.assertEqual(result["missing_indexes"][0], 501)
                self.assertTrue(result["missing_truncated"])
                self.assertEqual(result["next_offset"], 200)

    def test_context_budget_and_continuation_count_hits_not_context(self):
        rows = [{"index": i, "pre_dst": str(i)} for i in range(1, 1001)]
        args = {"filename": "a.json", "index": "100-900", "context": 20, "only_preceding": False}
        first = _read_transl_cache_entries(self.runner(rows), args)
        self.assertLessEqual(first["returned"], 200)
        second = _read_transl_cache_entries(self.runner(rows), {**args, "offset": first["next_offset"]})
        hits1 = [r["index"] for r in first["entries"] if isinstance(r["index"], int)]
        hits2 = [r["index"] for r in second["entries"] if isinstance(r["index"], int)]
        self.assertEqual(hits2[0], hits1[-1] + 1)
        text = _render_tool_result_table("read_transl_cache", first)
        self.assertIn(f"offset={first['next_offset']} 继续", text)

    def test_plain_input_and_output_reads_support_offset(self):
        rows = [{"index": i, "pre_src": str(i)} for i in range(1, 70)]
        for tool in (_tool_read_input_file, _tool_read_output):
            result = tool(self.runner(rows), {"filename": "a.json"})
            self.assertEqual(result["returned"], 30)
            next_page = tool(self.runner(rows), {"filename": "a.json", "offset": result["next_offset"]})
            self.assertEqual(next_page["entries"][0]["index"], 31)

    def test_plain_cache_read_continuation_follows_stable_order(self):
        rows = [{"index": i, "pre_src": str(i)} for i in range(1, 70)]
        for order, expected in (("name", 31), ("reverse", 39)):
            first = _read_transl_cache_entries(self.runner(rows), {"filename": "a.json", "order": order})
            second = _read_transl_cache_entries(self.runner(rows), {
                "filename": "a.json", "order": order, "offset": first["next_offset"],
            })
            self.assertEqual(second["entries"][0]["index"], expected)
        sampled = _read_transl_cache_entries(self.runner(rows), {"filename": "a.json", "order": "even"})
        self.assertNotIn("next_offset", sampled)


class ListAndNamePagingTests(unittest.TestCase):
    def test_file_lists_page_all_matching_files_in_stable_orders(self):
        files = [{"name": f"{i:04d}.json", "size": i, "sentences": 1} for i in range(1001)]
        runner = SimpleNamespace(state=SimpleNamespace(config_file_name="config.yaml"), _project_id=lambda: "proj",
                                 _http_get=lambda url: {"input_files": files, "files": files})
        for tool, key in ((_tool_list_input_files, "input_files"), (_tool_list_transl_cache, "cache_files")):
            for order in ("name", "size_asc", "size_desc"):
                with self.subTest(tool=tool.__name__, order=order):
                    offset, seen = 0, []
                    while True:
                        result = tool(runner, {"order": order, "limit": 500, "offset": offset})
                        seen.extend(row["name"] for row in result[key])
                        if not result["has_more"]:
                            break
                        offset = result["next_offset"]
                    expected = [row["name"] for row in files]
                    self.assertEqual(seen, expected[::-1] if order == "size_desc" else expected)

    def test_only_missing_filters_after_gpt_overlay_before_paging(self):
        runner = _NameRunner(names=[{"src_name": "A", "dst_name": ""}, {"src_name": "B", "dst_name": ""},
                                    {"src_name": "C", "dst_name": ""}, {"src_name": "D", "dst_name": "Saved"}],
                             config=_config(True), gpt_dict_files=["gpt_dict.txt"], dict_contents=_dict_file("A\tAuto"))
        result = _tool_get_name_table(runner, {"only_missing": True, "limit": 1})
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["missing_total"], 2)
        self.assertEqual(result["still_empty"], ["B"])
        next_page = _tool_get_name_table(runner, {"only_missing": True, "limit": 1, "offset": result["next_offset"]})
        self.assertEqual(next_page["still_empty"], ["C"])
        found = _tool_get_name_table(runner, {"query": "AUTO"})
        self.assertEqual(found["names"][0]["src_name"], "A")
        self.assertEqual(runner.writes, [])

    def test_name_defaults_and_empty_queries_do_not_echo_full_table(self):
        runner = _NameRunner(names=[{"src_name": str(i), "dst_name": ""} for i in range(1001)])
        result = _tool_get_name_table(runner, {})
        self.assertEqual(len(result["names"]), 100)
        self.assertEqual(len(result["still_empty"]), 100)
        self.assertEqual(result["missing_total"], 1001)
        empty = _tool_get_name_table(runner, {"query": "missing"})
        self.assertEqual(empty["matched"], 0)
        self.assertIn("本页没有", _render_tool_result_table("get_name_table", empty))

    def test_schemas_expose_defaults_and_paging(self):
        schemas = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in AGENT_TOOLS}
        self.assertEqual(schemas["save_dict"]["action"]["default"], "patch")
        self.assertEqual(schemas["save_name_table"]["mode"]["default"], "patch")
        for name in ("get_name_table", "list_input_files", "read_input_file", "read_output", "read_transl_cache"):
            self.assertTrue({"limit", "offset"} <= schemas[name].keys())


if __name__ == "__main__":
    unittest.main()
