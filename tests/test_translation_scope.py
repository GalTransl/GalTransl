"""片段翻译走真实解析、分块、缓存和输出，只替换外部模型调用。"""

import asyncio
from collections import Counter
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

import yaml

from GalTransl import CACHE_FOLDERNAME, INPUT_FOLDERNAME, OUTPUT_FOLDERNAME
from GalTransl.Agent.models import AgentState, AgentToolError
from GalTransl.Agent.tools.jobs import _tool_start_translation
from GalTransl.Agent.tool_operations import tool_operation
from GalTransl.Cache import save_transCache_to_json
from GalTransl.Service import JobSpec, run_job_async
from GalTransl.TranslationScope import SentenceRange, parse_sentence_range
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import RUNTIME_REGISTRY, RuntimeProgressCache, encode_project_dir


class SentenceRangeTests(unittest.TestCase):
    def test_half_open_range_is_canonicalized(self):
        self.assertEqual(parse_sentence_range(" 0 - 150 ", ["a.json"], "auto-translate"), SentenceRange(0, 150))
        self.assertIsNone(parse_sentence_range(None, [], "GenDic"))

    def test_invalid_or_ineffective_range_is_rejected(self):
        for value in ("", "1", "0-0", "150-0", "-1-10", "0-1,2-3", 100, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_sentence_range(value, ["a.json"], "auto-translate")
        with self.assertRaises(ValueError):
            parse_sentence_range("0-100", [], "auto-translate")
        for translator in ("GenDic", "dump-name", "rebuilda", "rebuildr"):
            with self.subTest(translator=translator), self.assertRaises(ValueError):
                parse_sentence_range("0-100", ["a.json"], translator)
        with self.assertRaises(AgentToolError):
            tool_operation("translation", {"action": "status", "sentence_range": "0-100"})

    def test_agent_forwards_shared_range_for_all_selected_files(self):
        posts = []
        runner = SimpleNamespace(
            state=AgentState(project_dir="test", config_file_name="config.yaml"),
            _http_post=lambda path, body: posts.append(body) or {"job_id": "j", "status": "pending"},
        )
        result = _tool_start_translation(runner, {"files": ["a.json", "sub/b.json"], "sentence_range": " 0 - 100 "})
        self.assertEqual(posts[0]["input_files"], ["a.json", "sub/b.json"])
        self.assertEqual(posts[0]["sentence_range"], "0-100")
        self.assertEqual(result["sentence_range"], "0-100")


class RecordingModel:
    def __init__(self):
        self.requests = []
        self.translated_sources = []

    async def batch_translate(self, filename, cache_path, trans_list, batch_size, **kwargs):
        self.requests.append([tran.pre_src for tran in trans_list])
        for tran in kwargs["translist_unhit"]:
            self.translated_sources.append(tran.pre_src)
            tran.pre_dst = tran.post_dst = "译:" + tran.pre_src
            tran.trans_by = "test-model"
        await save_transCache_to_json(trans_list, cache_path)

    def clean_up(self):
        pass

    async def shutdown(self):
        pass


class PartialTranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="galtransl-translation-scope-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project / INPUT_FOLDERNAME).mkdir()
        self.config = {
            "common": {"language": "ja2zh-cn", "workersPerProject": 1, "splitFile": "no",
                       "gpt.numPerRequestTranslate": 16, "loggingLevel": "warning", "saveLog": False},
            "dictionary": {"defaultDictFolder": "Dict", "preDict": [], "postDict": [], "gpt.dict": []},
            "plugin": {"filePlugin": "file_galtransl_json", "textPlugins": [], "problemPlugins": []},
            "backendSpecific": {"OpenAI-Compatible": {"checkAvailable": False, "tokens": [
                {"token": "test-only", "endpoint": "http://127.0.0.1:1", "modelName": "test-model"},
            ]}},
        }
        self.save_config()
        self.write_input("a.json", 180)
        self.write_input("b.json", 130)
        self.write_input("short.json", 3)
        self.model = RecordingModel()

    def save_config(self):
        (self.project / "config.yaml").write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    def write_input(self, name, count, explicit_index=False):
        path = self.project / INPUT_FOLDERNAME / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([
            {"message": f"{name}:{number}", **({"index": 1000 + number} if explicit_index else {})}
            for number in range(count)
        ]), encoding="utf-8")

    async def translate(self, files, sentence_range=None):
        async def model_factory(config):
            return self.model
        spec = JobSpec(project_dir=str(self.project), translator="auto-translate", input_files=files,
                       sentence_range=sentence_range)
        with patch("GalTransl.Frontend.LLMTranslate.init_gptapi", model_factory):
            return await run_job_async(spec)

    def messages(self, name):
        return [row["message"] for row in json.loads((self.project / OUTPUT_FOLDERNAME / name).read_text(encoding="utf-8"))]

    def progress(self):
        runtime = RUNTIME_REGISTRY.get_runtime_snapshot(str(self.project))
        return RuntimeProgressCache().get_progress(
            str(self.project), runtime["file_totals"], runtime["cache_file_display_map"],
            cache_file_selected_indexes=runtime["cache_file_selected_indexes"],
        )

    async def test_trial_translates_first_100_per_file_and_full_run_reuses_every_cached_sentence(self):
        state = await self.translate(["a.json", "b.json"], SentenceRange(0, 100))
        self.assertEqual(state.status, "completed", state.error)
        sent = [text for request in self.model.requests for text in request]
        self.assertEqual(Counter(sent), Counter(f"{name}:{i}" for name in ("a.json", "b.json") for i in range(100)))
        self.assertEqual(self.messages("a.json"), [f"译:a.json:{i}" if i < 100 else f"a.json:{i}" for i in range(180)])
        self.assertFalse((self.project / OUTPUT_FOLDERNAME / "short.json").exists())
        self.assertEqual((self.progress()["total"], self.progress()["translated"]), (200, 200))
        self.model.requests.clear()
        self.model.translated_sources.clear()
        state = await self.translate(["a.json", "b.json"])
        self.assertEqual(state.status, "completed", state.error)
        self.assertEqual(Counter(self.model.translated_sources), Counter(
            [f"a.json:{i}" for i in range(100, 180)] + [f"b.json:{i}" for i in range(100, 130)]
        ))
        self.assertEqual(self.messages("a.json"), [f"译:a.json:{i}" for i in range(180)])
        self.assertEqual((self.progress()["total"], self.progress()["translated"]), (310, 310))

    async def test_middle_range_uses_parse_position_and_preserves_old_translations_with_retranslation_key(self):
        self.write_input("a.json", 180, explicit_index=True)
        state = await self.translate(["a.json"])
        self.assertEqual(state.status, "completed", state.error)
        self.config["common"]["retranslKey"] = ["a.json"]
        self.config["common"]["retranslFail"] = True
        self.save_config()
        self.model.requests.clear()
        state = await self.translate(["a.json"], SentenceRange(50, 100))
        self.assertEqual(state.status, "completed", state.error)
        sent = [text for request in self.model.requests for text in request]
        self.assertEqual(sent, [f"a.json:{i}" for i in range(50, 100)])
        self.assertEqual(self.messages("a.json"), [f"译:a.json:{i}" for i in range(180)])
        self.assertEqual((self.progress()["total"], self.progress()["translated"]), (50, 50))
        rows = json.loads((self.project / CACHE_FOLDERNAME / "a.json").read_text(encoding="utf-8"))
        self.assertEqual([row["index"] for row in rows], list(range(1000, 1180)))

    async def test_count_split_with_overlap_keeps_original_chunks_and_range_boundaries(self):
        self.config["common"].update({"splitFile": "Num", "splitFileNum": 40, "splitFileCrossNum": 5})
        self.save_config()
        state = await self.translate(["a.json"], SentenceRange(30, 100))
        self.assertEqual(state.status, "completed", state.error)
        sent = [text for request in self.model.requests for text in request]
        self.assertEqual(set(sent), {f"a.json:{i}" for i in range(30, 100)})
        self.assertEqual(self.messages("a.json"), [f"译:a.json:{i}" if 30 <= i < 100 else f"a.json:{i}" for i in range(180)])
        runtime = RUNTIME_REGISTRY.get_runtime_snapshot(str(self.project))
        self.assertEqual(runtime["file_totals"]["a.json"], 70)
        self.assertIn("a.json_4.json", runtime["cache_file_display_map"])

    async def test_short_file_clips_end_and_missing_or_empty_selection_fails(self):
        state = await self.translate(["short.json"], SentenceRange(0, 100))
        self.assertEqual(state.status, "completed", state.error)
        self.assertEqual(self.messages("short.json"), [f"译:short.json:{i}" for i in range(3)])
        self.model.requests.clear()
        for files, scope in ((["short.json"], SentenceRange(3, 100)), (["a.json", "missing.json"], SentenceRange(0, 100))):
            state = await self.translate(files, scope)
            self.assertEqual(state.status, "failed")
        self.assertEqual(self.model.requests, [])

    async def test_nested_same_name_is_selected_by_relative_path(self):
        self.write_input("sub/a.json", 4)
        state = await self.translate(["sub/a.json"], SentenceRange(0, 2))
        self.assertEqual(state.status, "completed", state.error)
        self.assertEqual(self.model.requests, [["sub/a.json:0", "sub/a.json:1"]])
        self.assertFalse((self.project / OUTPUT_FOLDERNAME / "a.json").exists())

    async def test_equal_split_reuses_middle_slice_in_full_run(self):
        self.config["common"].update({"splitFile": "Equal", "splitFileNum": 4})
        self.save_config()
        state = await self.translate(["a.json"], SentenceRange(50, 100))
        self.assertEqual(state.status, "completed", state.error)
        self.assertEqual(Counter(self.model.translated_sources), Counter(f"a.json:{i}" for i in range(50, 100)))
        self.model.translated_sources.clear()
        state = await self.translate(["a.json"])
        self.assertEqual(state.status, "completed", state.error)
        self.assertEqual(Counter(self.model.translated_sources), Counter(f"a.json:{i}" for i in range(180) if not 50 <= i < 100))

    async def test_progress_does_not_count_cached_sentences_outside_range_and_refreshes_scope(self):
        self.assertEqual((await self.translate(["a.json"])).status, "completed")
        self.config["common"]["retranslKey"] = ["a.json"]
        self.save_config()
        snapshots = []

        class InspectingModel(RecordingModel):
            async def batch_translate(model, filename, cache_path, trans_list, batch_size, **kwargs):
                runtime = RUNTIME_REGISTRY.get_runtime_snapshot(str(self.project))
                snapshots.append(RuntimeProgressCache().get_progress(
                    str(self.project), runtime["file_totals"], runtime["cache_file_display_map"],
                    retran_key="a.json", cache_file_selected_indexes=runtime["cache_file_selected_indexes"],
                ))
                await super().batch_translate(filename, cache_path, trans_list, batch_size, **kwargs)

        self.model = InspectingModel()
        self.assertEqual((await self.translate(["a.json"], SentenceRange(50, 100))).status, "completed")
        self.assertEqual((snapshots[0]["total"], snapshots[0]["translated"]), (50, 0))
        tracker = RuntimeProgressCache()
        first = tracker.get_progress(str(self.project), {"a.json": 50}, {"a.json": "a.json"},
                                     cache_file_selected_indexes={"a.json": [str(i) for i in range(51, 101)]})
        second = tracker.get_progress(str(self.project), {"a.json": 10}, {"a.json": "a.json"},
                                      cache_file_selected_indexes={"a.json": [str(i) for i in range(1, 11)]})
        self.assertEqual((first["translated"], second["translated"]), (50, 10))

    async def test_http_job_api_applies_range_and_runtime_reports_trial_only(self):
        registry = JobRegistry(max_workers=1)
        self.addCleanup(registry._executor.shutdown)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(registry))
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_port}"

        async def model_factory(config):
            return self.model

        with patch("GalTransl.Frontend.LLMTranslate.init_gptapi", model_factory):
            request = Request(base + "/api/jobs", data=json.dumps({
                "project_dir": str(self.project), "translator": "auto-translate",
                "input_files": ["a.json", "b.json"], "sentence_range": "0-100",
            }).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                job = json.load(response)
            for attempt in range(100):
                current = registry.get_job(job["job_id"])
                if current["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(current["status"], "completed", current["error"])
            project_id = encode_project_dir(str(self.project))
            with urlopen(base + f"/api/projects/{project_id}/runtime", timeout=10) as response:
                runtime = json.load(response)
            self.assertEqual((runtime["summary"]["total"], runtime["summary"]["translated"]), (200, 200))

    async def test_progress_scope_change_excludes_cached_translations_from_another_slice(self):
        self.assertEqual((await self.translate(["a.json"], SentenceRange(50, 100))).status, "completed")
        tracker = RuntimeProgressCache()
        first = tracker.get_progress(str(self.project), {"a.json": 50}, {"a.json": "a.json"},
                                     cache_file_selected_indexes={"a.json": [str(i) for i in range(51, 101)]})
        second = tracker.get_progress(str(self.project), {"a.json": 10}, {"a.json": "a.json"},
                                      cache_file_selected_indexes={"a.json": [str(i) for i in range(1, 11)]})
        self.assertEqual((first["translated"], second["translated"]), (50, 0))


if __name__ == "__main__":
    unittest.main()
