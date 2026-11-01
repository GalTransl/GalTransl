"""缓存搜索的同条目 AND、精确字段、HTTP 校验及有界续查。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

from GalTransl import CACHE_FOLDERNAME
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir
from GalTransl.Agent.tools.find_replace import _tool_search_replace_cache
from GalTransl.Agent.tools.cache import _tool_patch_transl_cache
from GalTransl.Agent.tools.render_md import _render_tool_result_table
from GalTransl.Agent.permissions import _tool_risk


class CacheSearchConditionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="cache-search-conditions-")
        cls.project = cls.tmp.name
        os.makedirs(os.path.join(cls.project, CACHE_FOLDERNAME))
        with open(os.path.join(cls.project, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("common:\n  language: zh-cn\n")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/projects/{encode_project_dir(cls.project)}/cache/search"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def setUp(self):
        self.write([
            {"index": 1, "pre_src": "原始词", "post_src": "悶え ABC", "pre_dst": "挣扎", "proofread_dst": "润色"},
            {"index": 2, "post_src": "悶え", "pre_dst": "呻吟"},
            {"index": 3, "post_src": "普通", "pre_dst": "挣扎"},
            {"index": 4, "post_src": "悶え", "pre_dst": "", "proofread_dst": "挣扎"},
            {"index": 5, "post_jp": "悶え", "pre_zh": "挣扎"},
        ])

    def write(self, entries):
        with open(os.path.join(self.project, CACHE_FOLDERNAME, "a.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def search(self, **body):
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def agent(self):
        base_url = self.url.split("/api/", 1)[0]

        def request(path, body=None):
            query = urllib.request.Request(base_url + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(query, timeout=10) as response:
                return json.load(response)

        return SimpleNamespace(state=SimpleNamespace(config_file_name="config.yaml", project_dir=self.project),
            _model="test-agent", _project_id=lambda: encode_project_dir(self.project),
            _http_get=request, _http_post=request)

    def test_agent_preview_and_replace_share_role_scope_and_leave_preview_untouched(self):
        self.write([
            {"index": 1, "name": "シャニ", "pre_src": "原文", "post_src": "原文", "pre_dst": "Old OLD", "proofread_dst": "校对稿", "proofread_comment": "待处理", "trans_by": "original"},
            {"index": 2, "name": "アンバー", "post_src": "原文", "pre_dst": "Old OLD"},
        ])
        path = Path(self.project, CACHE_FOLDERNAME, "a.json")
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        agent = self.agent()
        args = {"query": "old", "files": "a.json", "conditions": [{"field": "name", "query": "^シャニ$", "options": {"re": True}}]}
        found = _tool_search_replace_cache(agent, {**args, "action": "search"})
        self.assertEqual([row["index"] for row in found["results"]], [1])
        preview = _tool_search_replace_cache(agent, {**args, "action": "replace", "replacement": "新", "clear_comment": True, "dry_run": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual((preview["updated"], preview["would_update"], preview["matches"]), (0, 1, 2))
        text = _render_tool_result_table("replace_cache", preview)
        self.assertIn("尚未写入", text)
        self.assertIn("Old OLD", text)
        self.assertIn("proofread_dst", text)
        applied = _tool_search_replace_cache(agent, {**args, "action": "replace", "replacement": "新", "clear_comment": True})
        self.assertEqual(applied["changes"], preview["changes"])
        self.assertEqual(applied["updated"], 1)
        saved = json.loads(path.read_bytes())
        self.assertEqual(saved[0]["pre_dst"], "新 新")
        self.assertEqual(saved[0]["proofread_dst"], "校对稿")
        self.assertEqual(saved[0]["proofread_comment"], "")
        self.assertEqual(saved[0]["trans_by"], "test-agent")
        self.assertEqual(saved[1]["pre_dst"], "Old OLD")
        self.assertIn("输出不会采用", _render_tool_result_table("replace_cache", applied))

    def test_agent_glob_paging_and_exact_hit_fields(self):
        self.write([{"index": i, "name": "シャニ", "post_src": "送译", "pre_src": "隐藏原文" + "长" * 100, "pre_dst": "普通译文", "proofread_dst": "隐藏校对稿"} for i in range(1, 151)])
        Path(self.project, CACHE_FOLDERNAME, "b.json").write_text(json.dumps([{"index": 1, "pre_dst": "普通译文"}]), encoding="utf-8")
        agent = self.agent()
        args = {"action": "search", "files": ["a*.json", "a.json"], "conditions": [{"field": "name", "query": "シャニ"}, {"field": "pre_src", "query": "隐藏"}, {"field": "proofread_dst", "query": "隐藏"}], "limit": 100}
        indexes = []
        offset = 0
        while True:
            page = _tool_search_replace_cache(agent, {**args, "offset": offset})
            text = _render_tool_result_table("search_cache", page)
            self.assertLessEqual(len(text), 10000)
            self.assertEqual(page["total"], 150)
            self.assertIn("隐藏原文", text)
            self.assertIn("隐藏校对稿", text)
            self.assertIn("matched_fields", text)
            indexes.extend(row["index"] for row in page["results"])
            if not page["has_more"]:
                break
            self.assertIn(f"offset={page['next_offset']} 继续", text)
            self.assertIn("字符预算", text)
            offset = page["next_offset"]
        self.assertEqual(indexes, list(range(1, 151)))

    def test_agent_match_case_and_literal_replacement(self):
        self.write([{"index": 1, "name": ["シャニ", "アンバー"], "post_src": "原文", "pre_dst": "OLD old"}])
        agent = self.agent()
        args = {"query": "old", "files": "a.json", "match_case": True, "conditions": [{"field": "name", "query": "シャニ"}]}
        found = _tool_search_replace_cache(agent, {**args, "action": "search"})
        self.assertEqual(found["total"], 1)
        result = _tool_search_replace_cache(agent, {**args, "action": "replace", "replacement": r"\g<1>"})
        self.assertEqual(result["matches"], 1)
        saved = json.loads(Path(self.project, CACHE_FOLDERNAME, "a.json").read_bytes())
        self.assertEqual(saved[0]["pre_dst"], r"OLD \g<1>")

    def test_regex_case_options_apply_to_search_and_replacement_filters(self):
        entries = [
            {"index": 1, "post_src": "ABC", "pre_dst": "old"},
            {"index": 2, "post_src": "abc", "pre_dst": "old"},
            {"index": 3, "post_src": "ABC", "pre_dst": "keep"},
        ]
        self.write(entries)
        agent = self.agent()
        for options, expected in (({"re": True}, [1, 2]), ({"re": True, "match_case": False}, [1, 2]), ({"re": True, "match_case": True}, [2])):
            with self.subTest(options=options):
                args = {"files": "a.json", "query": "old", "conditions": [
                    {"field": "post_src", "query": "^abc$", "options": options},
                ]}
                found = _tool_search_replace_cache(agent, {**args, "action": "search"})
                self.assertEqual([row["index"] for row in found["results"]], expected)
                preview = _tool_search_replace_cache(agent, {
                    **args, "action": "replace", "replacement": "new", "dry_run": True,
                })
                self.assertEqual(preview["matched_entries"], len(expected))
                self.assertEqual(preview["would_update"], len(expected))
                applied = _tool_search_replace_cache(agent, {
                    **args, "action": "replace", "replacement": "new",
                })
                self.assertEqual(applied["changes"], preview["changes"])
                saved = json.loads(Path(self.project, CACHE_FOLDERNAME, "a.json").read_bytes())
                self.assertEqual([row["index"] for row in saved if row["pre_dst"] == "new"], expected)
                self.write(entries)

    def test_conditions_filter_records_but_primary_query_selects_replacement_text(self):
        entries = [
            {"index": 1, "post_src": "原文", "pre_dst": "flag old old"},
            {"index": 2, "post_src": "原文", "pre_dst": "flag keep"},
            {"index": 3, "post_src": "原文", "pre_dst": "plain old"},
        ]
        self.write(entries)
        agent = self.agent()
        args = {"files": "a.json", "conditions": [{"field": "pre_dst", "query": "^flag", "options": {"re": True}}]}
        selected = _tool_search_replace_cache(agent, {**args, "action": "search"})
        self.assertEqual([row["index"] for row in selected["results"]], [1, 2])
        found = _tool_search_replace_cache(agent, {**args, "action": "search", "query": "old"})
        self.assertEqual([row["index"] for row in found["results"]], [1])
        preview = _tool_search_replace_cache(agent, {
            **args, "action": "replace", "query": "old", "replacement": "new", "dry_run": True,
        })
        self.assertEqual((preview["matched_entries"], preview["matches"], preview["would_update"]), (1, 2, 1))
        applied = _tool_search_replace_cache(agent, {
            **args, "action": "replace", "query": "old", "replacement": "new",
        })
        self.assertEqual(applied["changes"], preview["changes"])
        self.assertEqual((applied["matched_entries"], applied["matches"], applied["updated"]), (1, 2, 1))
        saved = json.loads(Path(self.project, CACHE_FOLDERNAME, "a.json").read_bytes())
        self.assertEqual([row["pre_dst"] for row in saved], ["flag new new", "flag keep", "plain old"])

    def test_preview_and_execution_report_partial_file_success(self):
        self.write([{"index": 1, "post_src": "原文", "pre_dst": "old"}])
        directory = Path(self.project, CACHE_FOLDERNAME)
        (directory / "invalid.json").write_text(json.dumps([
            {"index": "invalid", "post_src": "原文", "pre_dst": "old old"},
        ]), encoding="utf-8")
        (directory / "unmatched.json").write_text(json.dumps([
            {"index": 1, "post_src": "原文", "pre_dst": "keep"},
        ]), encoding="utf-8")
        before = {name: (directory / name).read_bytes() for name in ("a.json", "invalid.json", "unmatched.json")}
        agent = self.agent()
        args = {"action": "replace", "query": "old", "replacement": "new",
                "files": ["a.json", "invalid.json", "unmatched.json"]}
        preview = _tool_search_replace_cache(agent, {**args, "dry_run": True})
        self.assertEqual({name: (directory / name).read_bytes() for name in before}, before)
        applied = _tool_search_replace_cache(agent, args)
        self.assertEqual(applied["changes"], preview["changes"])
        self.assertEqual((preview["would_update"], applied["updated"]), (1, 1))
        for result in (preview, applied):
            self.assertEqual(result["status"], "partial")
            self.assertEqual((result["matched_entries"], result["matches"], result["failed_files"], result["unmatched_files"]), (2, 3, 1, 1))
            text = _render_tool_result_table("replace_cache", result)
            self.assertIn("部分", text)
            self.assertIn("1 个文件替换失败", text)
            self.assertIn("invalid.json", text)
            self.assertNotIn("unmatched.json", text)
        self.assertEqual(json.loads((directory / "a.json").read_bytes())[0]["pre_dst"], "new")
        self.assertEqual({name: (directory / name).read_bytes() for name in ("invalid.json", "unmatched.json")},
                         {name: before[name] for name in ("invalid.json", "unmatched.json")})

    def test_set_warns_only_when_proofread_remains_effective(self):
        self.write([{"index": 1, "post_src": "原文", "pre_dst": "旧", "proofread_dst": "校对稿"}])
        agent = self.agent()
        result = _tool_patch_transl_cache(agent, {"action": "set", "filename": "a.json", "patches": [{"index": 1, "pre_dst": "新"}]})
        self.assertEqual(result["files"][0]["shadowed_indexes"], [1])
        result = _tool_patch_transl_cache(agent, {"action": "set", "filename": "a.json", "patches": [{"index": 1, "pre_dst": "最终", "proofread_dst": ""}]})
        self.assertEqual(result["files"][0]["shadowed_indexes"], [])

    def test_preview_permission_is_read_only_and_execution_is_edit(self):
        self.assertEqual(_tool_risk("search_cache", {"action": "search"}), "read")
        self.assertEqual(_tool_risk("replace_cache", {"dry_run": True}), "read")
        self.assertEqual(_tool_risk("replace_cache", {"dry_run": False}), "edit")
        self.assertEqual(_tool_risk("replace_cache", {"dry_run": "true"}), "edit")

    def test_agent_invalid_and_retired_parameters_do_not_write(self):
        from GalTransl.Agent.models import AgentToolError
        agent = self.agent()
        path = Path(self.project, CACHE_FOLDERNAME, "a.json")
        before = path.read_bytes()
        cases = [
            {"action": "replace", "query": "挣扎", "replacement": "新", "replace_all": True},
            {"action": "replace", "query": "挣扎", "replacement": "新", "replace_all": False},
            {"action": "replace", "query": "挣扎", "replacement": "新", "field": "name"},
            {"action": "replace", "query": "挣扎", "replacement": "新", "offset": 1},
            {"action": "replace", "query": "挣扎", "replacement": "新", "dry_run": "true"},
            {"action": "replace", "query": "挣扎", "replacement": "新", "clear_comment": "false"},
            {"action": "search", "query": "挣扎", "fields": ["name"]},
            {"action": "search", "query": "挣扎", "filename": "a.json"},
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(AgentToolError):
                _tool_search_replace_cache(agent, args)
        self.assertEqual(path.read_bytes(), before)

    def test_conditions_match_the_same_entry_and_old_keys(self):
        out = self.search(conditions=[{"field": "post_src", "query": "悶え"}, {"field": "pre_dst", "query": "挣扎"}])
        self.assertEqual([r["index"] for r in out["results"]], [1, 5])
        self.assertTrue(all(r["match_src"] and r["match_dst"] for r in out["results"]))

    def test_primary_query_is_an_additional_condition_and_case_insensitive(self):
        out = self.search(query="abc", field="post_src", conditions=[{"field": "pre_dst", "query": "挣扎"}])
        self.assertEqual([r["index"] for r in out["results"]], [1])

    def test_exact_fields_do_not_fall_back_to_other_columns(self):
        out = self.search(conditions=[{"field": "post_src", "query": "原始词"}])
        self.assertEqual(out["total"], 0)
        out = self.search(conditions=[{"field": "pre_src", "query": "原始词"}])
        self.assertEqual([r["index"] for r in out["results"]], [1])
        out = self.search(query="挣扎", field="dst")
        self.assertIn(4, [r["index"] for r in out["results"]])

    def test_each_condition_can_use_regex_independently(self):
        out = self.search(conditions=[{"field": "post_src", "query": "^悶. ABC$", "options": {"re": True}},
                                      {"field": "pre_dst", "query": "挣扎"}])
        self.assertEqual([r["index"] for r in out["results"]], [1])

    def test_invalid_conditions_are_bad_requests(self):
        for conditions in ([], {}, [None], [{"field": "unknown", "query": "x"}],
                           [{"field": "pre_dst", "query": ""}],
                           [{"field": "pre_dst", "query": "[", "options": {"re": True}}],
                           [{"field": "pre_dst", "query": "x", "options": {"re": "false"}}],
                           [{"field": "pre_dst", "query": "x", "options": {"match_case": "false"}}],
                           [{"field": "pre_dst", "query": "x"}] * 21):
            with self.subTest(conditions=conditions):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.search(conditions=conditions)
                self.assertEqual(caught.exception.code, 400)

    def test_budgets_keep_all_matches_reachable_without_duplicates(self):
        self.write([{"index": i, "post_src": "hit", "pre_dst": "译文" * 100} for i in range(1, 206)])
        indexes = []
        offset = 0
        while True:
            out = self.search(query="hit", max_results=9999, offset=offset)
            self.assertLessEqual(len(out["results"]), 100)
            self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 10000)
            self.assertEqual(out["total"], 205)
            indexes.extend(r["index"] for r in out["results"])
            if not out["has_more"]:
                break
            self.assertGreater(out["next_offset"], offset)
            offset = out["next_offset"]
        self.assertEqual(indexes, list(range(1, 206)))

    def test_frontend_returns_all_matches_and_full_text_without_budgets(self):
        text = "译文" * 10000
        self.write([{"index": i, "post_src": "hit", "pre_dst": text} for i in range(1, 206)]
                   + [{"index": 206, "post_src": "hit", "pre_dst": "不匹配"}])
        out = self.search(query="hit", result_mode="all",
                          conditions=[{"field": "pre_dst", "query": "译文"}])
        self.assertEqual(out["total"], 205)
        self.assertEqual([r["index"] for r in out["results"]], list(range(1, 206)))
        self.assertTrue(all(r["pre_dst"] == text and not r.get("truncated") for r in out["results"]))
        self.assertFalse(out["has_more"])
        self.assertNotIn("truncated", out)

    def test_frontend_empty_results_are_complete(self):
        out = self.search(query="不存在", result_mode="all")
        self.assertEqual(out["results"], [])
        self.assertEqual(out["total"], 0)
        self.assertFalse(out["has_more"])

    def test_invalid_result_mode_is_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.search(query="hit", result_mode="unknown")
        self.assertEqual(caught.exception.code, 400)

    def test_context_counts_toward_both_budgets_and_paging_counts_only_hits(self):
        self.write([{"index": i, "post_src": "hit" if i % 5 == 0 else "context", "pre_dst": "译文" * 50}
                    for i in range(1, 121)])
        indexes = []
        offset = 0
        while True:
            out = self.search(query="hit", context=3, max_results=100, offset=offset)
            hits = [r for r in out["results"] if r["match_src"]]
            self.assertEqual(len(hits), out["returned_hits"])
            self.assertLessEqual(len(out["results"]), 100)
            self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 10000)
            indexes.extend(r["index"] for r in hits)
            if not out["has_more"]:
                break
            self.assertGreater(out["next_offset"], offset)
            offset = out["next_offset"]
        self.assertEqual(indexes, list(range(5, 121, 5)))

    def test_one_oversized_hit_returns_a_preview_and_advances(self):
        self.write([{"index": 1, "post_src": "hit" * 20000}, {"index": 2, "post_src": "hit"}])
        out = self.search(query="hit", context=2)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["next_offset"], 1)
        self.assertTrue(out["results"][0]["truncated"])
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 10000)
        self.assertEqual(self.search(query="hit", offset=1)["results"][0]["index"], 2)
