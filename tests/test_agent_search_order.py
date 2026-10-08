"""Search sampling must operate on all matches before limiting the response."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.tool_schemas import AGENT_TOOLS
from GalTransl.Agent.tools.ask import _tool_read_history_archive
from GalTransl.Agent.tools.cache import _tool_read_transl_cache
from GalTransl.Agent.tools.problems import _tool_list_problems
from GalTransl.Agent.tools.render_md import _md_render_search_input
from GalTransl.Agent.tools.search import _tool_search_input
from GalTransl.Search import parse_search_order, select_search_hits


class _Runner:
    def __init__(self):
        self.state = SimpleNamespace(config_file_name="config.yaml")
        self.posts = []
        self.rows = [
            {"filename": "a.json" if i < 5 else "b.json", "index": i, "post_src": "match", "pre_dst": "translated", "problem": "test", "trans_by": "model"}
            for i in range(1, 9)
        ]

    def _project_id(self):
        return "project"

    def _http_get(self, url):
        if "/problems?" in url:
            return {"problems": [dict(row) for row in self.rows], "total": len(self.rows)}
        return {"entries": [dict(row) for row in self.rows]}

    def _http_post(self, url, body):
        self.posts.append((url, body))
        return {"results": [], "total": 8}


class SearchOrderTests(unittest.TestCase):
    def test_even_covers_the_whole_range_and_reverse_pages_in_reverse_order(self):
        self.assertEqual(select_search_hits(list(range(9)), 3, 0, "even"), [0, 4, 8])
        self.assertEqual(select_search_hits(list(range(9)), 3, 2, "reverse"), [6, 5, 4])
        self.assertEqual(select_search_hits(list(range(9)), 3, 2, "even"), [2, 5, 8])

    def test_random_samples_the_full_remaining_population(self):
        with patch("GalTransl.Search.random.sample", side_effect=lambda population, count: population[-count:]) as sample:
            self.assertEqual(select_search_hits(list(range(9)), 2, 1, "random"), [7, 8])
        sample.assert_called_once_with(list(range(1, 9)), 2)

    def test_random_returns_unique_hits_and_respects_limits(self):
        for limit in (1, 5, 100):
            selected = select_search_hits(list(range(20)), limit, 0, "random")
            self.assertEqual(len(selected), min(limit, 20))
            self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(select_search_hits([1, 2], 2, 3, "random"), [])

    def test_invalid_order_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_search_order({"order": "unknown"})
        with self.assertRaises(AgentToolError):
            _tool_search_input(_Runner(), {"query": "match", "order": "unknown"})

    def test_agents_forward_order_and_show_sampling_guidance(self):
        for action in ("input", "cache"):
            runner = _Runner()
            args = {"query": "match", "order": "random", "limit": 2}
            result = _tool_search_input(runner, args) if action == "input" else _tool_read_transl_cache(runner, args)
            self.assertEqual(runner.posts[0][1]["order"], "random")
            self.assertEqual(runner.posts[0][1]["max_results"], 2)
            rendered = _md_render_search_input(result)
            self.assertIn("随机采样", rendered)
            self.assertIn("可再次随机采样", rendered)

    def test_problem_sampling_happens_after_scope_and_type_filtering(self):
        runner = _Runner()
        with patch("GalTransl.Search.random.sample", side_effect=lambda population, count: population[-count:]) as sample:
            result = _tool_list_problems(runner, {"problem_type": "test", "order": "random", "limit": 2}, allowed_files=["b.json"])
        self.assertEqual([row["index"] for row in result["problems"]], [7, 8])
        self.assertEqual(result["matched"], 4)
        self.assertEqual(len(sample.call_args.args[0]), 4)

    def test_cache_grep_sampling_uses_all_filtered_entries(self):
        runner = _Runner()
        result = _tool_read_transl_cache(runner, {"filename": "a.json", "grep": "match", "order": "even", "limit": 3})
        self.assertEqual([row["index"] for row in result["entries"]], [1, 4, 8])
        self.assertEqual(result["count"], 8)

    def test_archive_sampling_searches_later_chunks_and_counts_all_hits(self):
        runner = _Runner()
        chunks = {"chunk-0001.md": "match first\nmatch second", "chunk-0002.md": "match last"}
        runner._store = SimpleNamespace(list_chunks=lambda: [{"name": name} for name in chunks], read_chunk=chunks.get)
        with patch("GalTransl.Search.random.sample", side_effect=lambda population, count: population[-count:]):
            result = _tool_read_history_archive(runner, {"query": "match", "order": "random", "limit": 1})
        self.assertEqual(result["hits"][0]["chunk"], "chunk-0002.md")
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["has_more"])

    def test_all_search_schemas_expose_sampling(self):
        schemas = {tool["function"]["name"]: tool["function"]["parameters"]["properties"] for tool in AGENT_TOOLS}
        for tool in ("search_input", "read_transl_cache", "list_problems", "read_history_archive"):
            self.assertIn("random", schemas[tool]["order"]["enum"])
            self.assertIn("even", schemas[tool]["order"]["enum"])

    def test_random_search_keeps_the_context_row_budget(self):
        for action in ("input", "cache"):
            runner = _Runner()
            args = {"query": "match", "order": "random", "limit": 200, "context": 20, "only_preceding": False}
            if action == "input":
                _tool_search_input(runner, args)
            else:
                _tool_read_transl_cache(runner, args)
            self.assertEqual(runner.posts[0][1]["max_results"], 4)
