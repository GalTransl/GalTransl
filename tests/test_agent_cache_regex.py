"""Regex replacement uses the real cache planner; literals stay literal."""

import unittest

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tools.find_replace import _tool_search_replace_cache
from GalTransl.Search import parse_cache_search_conditions
from test_agent_replace_cache import _Runner


class RegexCacheTests(unittest.TestCase):
    def test_captures_preview_actual_changes_and_independent_condition(self):
        runner = _Runner({"a.json": [
            {"index": 1, "name": "対象", "pre_dst": "item-12 ITEM-34", "proofread_dst": "保留", "proofread_comment": "旧意见"},
            {"index": 2, "name": "その他", "pre_dst": "item-56"},
        ]})
        args = {"action": "replace", "files": "a.json", "query": r"item-(?P<number>\d+)",
                "replacement": r"编号\g<number>", "regex": True, "clear_comment": True,
                "conditions": [{"field": "name", "query": "^対象$", "options": {"re": True}}]}
        before = runner.disk["a.json"][0]["pre_dst"]
        preview = _tool_search_replace_cache(runner, {**args, "dry_run": True})
        self.assertEqual(runner.saved, [])
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], before)
        self.assertEqual((preview["matches"], preview["would_update"]), (2, 1))
        result = _tool_search_replace_cache(runner, args)
        self.assertEqual(result["changes"], preview["changes"])
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], "编号12 编号34")
        self.assertEqual(runner.disk["a.json"][0]["proofread_dst"], "保留")
        self.assertEqual(runner.disk["a.json"][0]["proofread_comment"], "")
        self.assertEqual(runner.disk["a.json"][1]["pre_dst"], "item-56")

    def test_literal_default_and_case_sensitive_regex(self):
        for regex, query, replacement, expected in (
            (False, "a.b", r"\1", r"\1 aXb"),
            (True, r"(a).b", r"\1", "a a"),
        ):
            with self.subTest(regex=regex):
                runner = _Runner({"a.json": [{"index": 1, "pre_dst": "a.b aXb"}]})
                _tool_search_replace_cache(runner, {"action": "replace", "files": "a.json", "query": query,
                                                  "replacement": replacement, "regex": regex})
                self.assertEqual(runner.disk["a.json"][0]["pre_dst"], expected)
        runner = _Runner({"a.json": [{"index": 1, "pre_dst": "OLD old"}]})
        result = _tool_search_replace_cache(runner, {"action": "replace", "files": "a.json", "query": "o.d",
                                                   "replacement": "new", "regex": True, "match_case": True})
        self.assertEqual(result["matches"], 1)
        self.assertEqual(runner.disk["a.json"][0]["pre_dst"], "OLD new")

    def test_zero_width_multiline_deletion_and_escaped_backslash(self):
        for query, replacement, before, after, matches in (
            (r"(?m)^", ">", "one\ntwo", ">one\n>two", 2),
            (r"\d+", "", "a12b34", "ab", 2),
            (r"(a)", r"\\\1", "a", r"\a", 1),
            (" ", "_", "a b", "a_b", 1),
        ):
            with self.subTest(query=query):
                runner = _Runner({"a.json": [{"index": 1, "pre_dst": before}]})
                result = _tool_search_replace_cache(runner, {"action": "replace", "files": "a.json", "query": query,
                                                           "replacement": replacement, "regex": True})
                self.assertEqual(result["matches"], matches)
                self.assertEqual(runner.disk["a.json"][0]["pre_dst"], after)

    def test_invalid_patterns_templates_and_boolean_fail_before_read_or_write(self):
        for overrides in ({"query": "["}, {"replacement": r"\2"}, {"replacement": r"\g<unknown>"},
                          {"replacement": r"\q"}, {"regex": "true"}, {"query": ""}):
            with self.subTest(overrides=overrides):
                runner = _Runner({"a.json": [{"index": 1, "pre_dst": "none"}]})
                with self.assertRaises(AgentToolError):
                    _tool_search_replace_cache(runner, {"action": "replace", "files": "a.json", "query": "(old)",
                                                      "replacement": "new", "regex": True, **overrides})
                self.assertEqual(runner.saved, [])
                self.assertEqual(runner.reads, [])

    def test_regex_whitespace_is_preserved_for_primary_and_filter_queries(self):
        for payload in ({"field": "pre_dst", "query": " ", "options": {"re": True}},
                        {"conditions": [{"field": "pre_dst", "query": r" word\ ", "options": {"re": True}}]}):
            condition = parse_cache_search_conditions(payload)[0]
            self.assertTrue(condition.matches(" word "))
            self.assertFalse(condition.matches("word"))


if __name__ == "__main__":
    unittest.main()
