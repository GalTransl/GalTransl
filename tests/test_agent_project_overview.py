import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl.Agent import session_store as ss
from GalTransl.Agent.runtime import (
    OVERVIEW_SECTIONS,
    AgentRunner,
    AgentRuntime,
    AgentState,
    AgentToolError,
    _annotate_config,
    _backend_overview,
    _backend_summary,
    _config_for_overview,
    _count_input_file_progress,
    _input_cache_matchers,
    _normalize_overview_include,
    _tool_get_project_overview,
)


class InputCacheMatcherTests(unittest.TestCase):
    """输入文件 → 缓存文件名的换算，须与 LLMTranslate 的命名规则一致。"""

    def test_json_input_single_and_chunked(self) -> None:
        singles, chunk_re = _input_cache_matchers("foo.json")
        self.assertIn("foo.json", singles)
        self.assertTrue(chunk_re.match("foo.json_0.json"))
        self.assertTrue(chunk_re.match("foo.json_1.json.append.jsonl"))

    def test_non_json_input_gets_json_suffix(self) -> None:
        singles, chunk_re = _input_cache_matchers("foo.ks")
        self.assertIn("foo.ks.json", singles)
        self.assertTrue(chunk_re.match("foo.ks_0.json"))
        # 非分块缓存名不应被当成“另一分块”
        self.assertIsNone(chunk_re.match("foo.ks.json"))

    def test_nested_path_separator_replaced(self) -> None:
        singles, _ = _input_cache_matchers("sub/foo.json")
        self.assertIn("sub-}foo.json", singles)


class AgentProjectOverviewFileCountTests(unittest.TestCase):
    """回归：overview 必须给出“已翻译文件数/未翻译文件数”。

    只翻了 1 个文件、但该文件的句数恰好等于缓存句数时，句数会显示 100%，
    不能因此判定项目翻完；文件级计数才是收尾依据。
    """

    def test_only_files_with_real_translation_count_as_translated(self) -> None:
        inputs = ["01.json", "02.json", "03.ks", "sub-01.ks"]
        progress_files = [
            {"filename": "01.json", "translated": 269},  # 单块 json
            {"filename": "02.json", "translated": 0},  # 有缓存但没有译文
            {"filename": "03.ks_0.json", "translated": 3},  # 多块非 json
            {"filename": "03.ks_1.json", "translated": 0},
            {"filename": "sub-01.ks.json", "translated": 5},
            {"filename": "ghost.json", "translated": 9},  # 输入目录外的多余缓存，不计入
        ]

        result = _count_input_file_progress(inputs, progress_files)

        self.assertEqual(result["files_total"], 4)
        self.assertEqual(result["files_translated"], 3)
        self.assertEqual(result["files_untranslated"], 1)

    def test_no_cache_means_all_untranslated(self) -> None:
        result = _count_input_file_progress(["a.json", "b.json"], [])
        self.assertEqual(
            result,
            {"files_total": 2, "files_translated": 0, "files_untranslated": 2},
        )


class OverviewConfigTests(unittest.TestCase):
    """「了解项目」返回的配置里不能带 backendSpecific（内含 API 令牌/端点）。"""

    def test_backend_specific_is_stripped(self) -> None:
        raw = {
            "common": {"workersPerProject": 1},
            "backendSpecific": {
                "OpenAI-Compatible": {"tokens": [{"token": "sk-secret", "endpoint": "https://x"}]}
            },
        }

        config = _config_for_overview(raw)

        self.assertNotIn("backendSpecific", config)
        self.assertEqual(config["common"], {"workersPerProject": 1})
        self.assertNotIn("sk-secret", str(config))

    def test_annotated_overview_config_has_no_backend_hints(self) -> None:
        """说明表里也不该再出现 backendSpecific（连带 section 提示与逐键说明）。"""
        config, descriptions = _annotate_config(
            _config_for_overview(
                {"backendSpecific": {"SakuraLLM": {"endpoints": ["http://127.0.0.1:8080"]}}, "common": {}}
            )
        )

        self.assertNotIn("backendSpecific", config)
        self.assertFalse([key for key in descriptions if key.startswith("backendSpecific")])

    def test_missing_or_malformed_config_is_empty(self) -> None:
        self.assertEqual(_config_for_overview(None), {})
        self.assertEqual(_config_for_overview("nope"), {})


class EffectiveBackendTests(unittest.TestCase):
    """「了解项目」要报实际生效的两份后端（agent = 本会话在用的，translator = 翻译任务
    会用的），各含配置名 / 类型 / 模型名，且不含地址与密钥。"""

    @staticmethod
    def _runner(agent_profile: dict, **state_fields: object) -> AgentRunner:
        state = AgentState()
        state.backend_profile_data = agent_profile
        for key, value in state_fields.items():
            setattr(state, key, value)
        return AgentRunner(state)

    def test_openai_compatible_reports_name_type_and_model(self) -> None:
        info = _backend_summary(
            {
                "OpenAI-Compatible": {
                    "tokens": [
                        {
                            "token": "sk-secret",
                            "endpoint": "https://api.deepseek.com",
                            "modelName": "deepseek-chat",
                        },
                        {
                            "token": "sk-second",
                            "endpoint": "https://openrouter.ai/api/v1",
                            "modelName": "other/model",
                        },
                    ]
                }
            },
            "DeepSeek 默认",
        )

        self.assertEqual(info["name"], "DeepSeek 默认")  # 前端送来的配置名
        self.assertEqual(info["type"], "OpenAI-Compatible")
        self.assertEqual(info["model"], "deepseek-chat")  # 取首个 token 的模型名
        self.assertNotIn("sk-secret", str(info))  # 密钥不外泄
        self.assertNotIn("api.deepseek.com", str(info))  # 地址不外泄

    def test_name_falls_back_to_section_without_profile_name(self) -> None:
        info = _backend_summary({"SakuraLLM": {"endpoints": ["http://127.0.0.1:8080"]}})

        self.assertEqual(info["name"], "SakuraLLM")  # 没配置名就用类型兜底
        self.assertEqual(info["model"], "")
        self.assertNotIn("127.0.0.1", str(info))

    def test_missing_profile_is_empty(self) -> None:
        self.assertEqual(_backend_summary({}), {"name": "", "type": "", "model": ""})

    def test_overview_reports_both_agent_and_translator(self) -> None:
        runner = self._runner(
            {"OpenAI-Compatible": {"tokens": [{"token": "sk-agent", "modelName": "agent-model"}]}},
            backend_profile_name="Agent 默认",
            translator_profile_name="翻译器默认",
            translator_profile_data={
                "OpenAI-Compatible": {"tokens": [{"token": "sk-trans", "modelName": "translator-model"}]}
            },
        )

        info = _backend_overview(runner)

        self.assertEqual(
            info["agent"],
            {"name": "Agent 默认", "type": "OpenAI-Compatible", "model": "agent-model"},
        )
        self.assertEqual(
            info["translator"],
            {"name": "翻译器默认", "type": "OpenAI-Compatible", "model": "translator-model"},
        )
        self.assertNotIn("sk-agent", str(info))
        self.assertNotIn("sk-trans", str(info))


class OverviewToolReturnTests(unittest.TestCase):
    """get_project_overview 的完整返回：配置里没有 backendSpecific，但多出实际后端。"""

    def test_return_shape_keeps_backend_and_drops_secrets(self) -> None:
        state = AgentState()
        state.project_dir = r"C:\proj"
        state.backend_profile_name = "Agent 默认"
        state.backend_profile_data = {
            "OpenAI-Compatible": {
                "tokens": [
                    {
                        "token": "sk-secret",
                        "endpoint": "https://api.deepseek.com",
                        "modelName": "deepseek-chat",
                    }
                ]
            }
        }
        # 翻译任务会用的那份（前端随 start/message 送来）
        state.translator_profile_name = "翻译器默认"
        state.translator_profile_data = {
            "OpenAI-Compatible": {"tokens": [{"token": "sk-trans", "modelName": "translate-model"}]}
        }
        runner = AgentRunner(state)

        def fake_get(path: str) -> dict:
            if "progress?config=" in path:
                return {"total": 10, "translated": 10, "problems": 0, "failed": 0, "files": []}
            if "/config?config=" in path:
                # 项目配置里那份（往往是旧值，且含密钥）
                return {
                    "config": {
                        "common": {"language": "zh-cn"},
                        "backendSpecific": {
                            "OpenAI-Compatible": {"tokens": [{"token": "sk-secret-old"}]}
                        },
                    }
                }
            if path.endswith("/files"):
                return {"input_files": []}
            raise AssertionError(f"未预期的请求: {path}")

        with patch.object(AgentRunner, "_http_get", lambda self, path: fake_get(path)):
            out = _tool_get_project_overview(runner, {})

        self.assertNotIn("backendSpecific", out["config"])
        self.assertEqual(out["config"]["common"], {"language": "zh-cn"})
        self.assertEqual(out["backend"]["agent"]["name"], "Agent 默认")
        self.assertEqual(out["backend"]["agent"]["model"], "deepseek-chat")
        self.assertEqual(out["backend"]["translator"]["name"], "翻译器默认")
        self.assertEqual(out["backend"]["translator"]["model"], "translate-model")
        # 地址、密钥一律不出现（项目配置里的旧 key 也不能漏出来）
        self.assertNotIn("sk-secret", str(out))
        self.assertNotIn("sk-trans", str(out))
        self.assertNotIn("api.deepseek.com", str(out))


class OverviewIncludeTests(unittest.TestCase):
    """按 include 分段返回：开局拿全，之后只取变化的部分。

    分段的关键不只是返回体变小，还在于**没要的分区不发那条 HTTP**——否则省不下来。
    """

    @staticmethod
    def _runner() -> AgentRunner:
        state = AgentState()
        state.project_dir = r"C:\proj"
        state.backend_profile_data = {"OpenAI-Compatible": {"tokens": [{"modelName": "m"}]}}
        return AgentRunner(state)

    @staticmethod
    def _fake_get(paths: list[str]):
        def fake_get(path: str) -> dict:
            paths.append(path)
            if "/progress" in path:
                return {"total": 10, "translated": 4, "problems": 1, "failed": 0, "files": []}
            if path.endswith("/files"):
                return {"input_files": [{"name": "01.json", "is_file": True}]}
            if "/config" in path:
                return {"config": {"common": {"language": "zh-cn"}}}
            raise AssertionError(f"未预期的请求: {path}")

        return fake_get

    def _call(self, args: dict) -> tuple[dict, list[str]]:
        paths: list[str] = []
        fake = self._fake_get(paths)
        with patch.object(AgentRunner, "_http_get", lambda _self, path: fake(path)):
            return _tool_get_project_overview(self._runner(), args), paths

    def test_default_returns_everything_without_note(self) -> None:
        out, _ = self._call({})
        self.assertEqual(sorted(out), sorted(OVERVIEW_SECTIONS))
        self.assertNotIn("note", out)  # 全量返回时不加"本次只返回了…"的提示

    def test_progress_only_skips_the_config_request(self) -> None:
        out, paths = self._call({"include": ["progress"]})
        self.assertEqual(sorted(out), ["note", "progress"])
        self.assertEqual(out["progress"]["total"], 10)
        self.assertFalse([p for p in paths if "/config" in p])

    def test_progress_and_backend_order_is_fixed(self) -> None:
        out, _ = self._call({"include": ["backend", "progress"]})
        # 返回顺序按固定顺序（progress 在前），与 include 的书写顺序无关
        self.assertEqual(list(out), ["progress", "backend", "note"])

    def test_config_without_descriptions(self) -> None:
        out, _ = self._call({"include": ["config"]})
        self.assertIn("config", out)
        self.assertNotIn("config_field_descriptions", out)

    def test_normalize_dedupes_and_orders(self) -> None:
        self.assertEqual(
            _normalize_overview_include({"include": ["config", "progress", "config"]}),
            ["progress", "config"],
        )
        self.assertEqual(_normalize_overview_include({}), list(OVERVIEW_SECTIONS))

    def test_bad_include_is_rejected(self) -> None:
        for bad in ({"include": ["progress", "nope"]}, {"include": []}, {"include": "progress"}):
            with self.assertRaises(AgentToolError):
                _normalize_overview_include(bad)


class BackendContextPlumbingTests(unittest.TestCase):
    """start/message 送来的后端上下文要落到 state 上，并在 message 时刷新。

    配置名只存在前端 localStorage，后端只能靠这两条请求拿到；用户中途改了默认配置
    时，随下一次 message 刷新即可（不送来的字段不该被清空）。
    """

    def setUp(self) -> None:
        self._root = tempfile.mkdtemp(prefix="agent-backendctx-root-")
        self._orig_root = ss.SESSIONS_ROOT
        ss.SESSIONS_ROOT = self._root
        self.project = os.path.join(tempfile.mkdtemp(prefix="agent-backendctx-proj-"), "MyGame")
        os.makedirs(self.project, exist_ok=True)

    def tearDown(self) -> None:
        ss.SESSIONS_ROOT = self._orig_root

    def test_start_and_message_store_backend_context(self) -> None:
        agent_profile = {
            "OpenAI-Compatible": {"tokens": [{"token": "sk-a", "modelName": "agent-model"}]}
        }
        translator_profile = {
            "OpenAI-Compatible": {"tokens": [{"token": "sk-t", "modelName": "translator-model"}]}
        }
        rt = AgentRuntime()
        sid = rt.create_session(self.project)["session_id"]

        with patch.object(AgentRunner, "run", lambda self: None):
            rt.start(
                self.project,
                "config.yaml",
                agent_profile,
                goal="第一轮",
                session_id=sid,
                backend_profile_name="Agent 默认",
                translator_profile_name="翻译器默认",
                translator_profile_data=translator_profile,
            )
            state = rt._get_state(self.project, sid)
            assert state is not None
            state.messages.append({"role": "user", "content": "第一轮"})
            self.assertEqual(state.backend_profile_name, "Agent 默认")
            self.assertEqual(state.translator_profile_name, "翻译器默认")
            self.assertEqual(
                state.translator_profile_data["OpenAI-Compatible"]["tokens"][0]["modelName"],
                "translator-model",
            )
            self.assertEqual(
                _backend_overview(rt._runners[rt._key(self.project)][sid])["translator"]["name"],
                "翻译器默认",
            )

            # 用户中途改了默认配置 → 下一条消息把新的名字带过来
            state.status = "awaiting_input"
            rt.message(self.project, "继续", sid, backend_profile_name="Agent 新默认")

        self.assertEqual(state.backend_profile_name, "Agent 新默认")
        # 这次没送翻译器信息 → 保留上一次的，不该被清空
        self.assertEqual(state.translator_profile_name, "翻译器默认")
        self.assertTrue(state.translator_profile_data)


if __name__ == "__main__":
    unittest.main()
