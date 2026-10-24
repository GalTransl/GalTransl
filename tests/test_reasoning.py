"""OpenAI-compatible reasoning effort is explicit and supports token overrides."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import AsyncOpenAI

from GalTransl.Agent.llm_backend import resolve_llm_backend
from GalTransl.Agent.models import AgentState
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Agent.subagent import SubAgentRunner
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl.COpenAI import COpenAITokenPool
from GalTransl.Reasoning import apply_reasoning_kwargs, build_reasoning_kwargs


class ReasoningConfigTests(unittest.TestCase):
    def test_unconfigured_options_are_not_sent(self):
        self.assertEqual(build_reasoning_kwargs({}), {})

    def test_effort_maps_to_the_request_field(self):
        for config, expected in (
            ({"reasoningEffort": "none"}, {"reasoning_effort": "none"}),
            ({"reasoningEffort": "high"}, {"reasoning_effort": "high"}),
        ):
            with self.subTest(config=config):
                self.assertEqual(build_reasoning_kwargs(config), expected)

    def test_removed_options_are_ignored(self):
        legacy = {"enableThinking": True, "thinkingType": "enabled"}
        self.assertEqual(build_reasoning_kwargs(legacy), {})
        result = build_reasoning_kwargs({
            **legacy,
            "reasoningEffort": "high",
        })

        self.assertEqual(result, {"reasoning_effort": "high"})

    def test_token_effort_overrides_global_effort(self):
        result = build_reasoning_kwargs(
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "none"},
        )

        self.assertEqual(result, {"reasoning_effort": "none"})

    def test_empty_token_values_follow_global_values(self):
        for value in (None, "", " "):
            with self.subTest(value=value):
                result = build_reasoning_kwargs(
                    {"reasoningEffort": "low"}, {"reasoningEffort": value},
                )
                self.assertEqual(result, {"reasoning_effort": "low"})

    def test_model_name_and_endpoint_do_not_infer_anything(self):
        for model, endpoint in (
            ("deepseek-reasoner", "https://api.deepseek.com"),
            ("Qwen3", "https://api.siliconflow.cn"),
            ("o3", "https://api.openai.com"),
            ("custom-alias", "https://proxy.example.com"),
        ):
            with self.subTest(model=model):
                pool = COpenAITokenPool(SimpleNamespace(getBackendConfigSection=lambda _: {
                    "tokens": [{"token": "sk-test", "modelName": model, "endpoint": endpoint}],
                }), "ForGal")
                self.assertEqual(pool.tokens[0][1].reasoning_kwargs, {})

    def test_none_effort_is_sent_explicitly_and_keeps_sampling(self):
        request = {"temperature": 0.6, "max_tokens": 1200}
        result = apply_reasoning_kwargs(request, {"reasoning_effort": "none"})
        self.assertEqual(result["reasoning_effort"], "none")
        self.assertEqual(result["temperature"], 0.6)
        self.assertEqual(result["max_completion_tokens"], 1200)
        self.assertEqual(request, {"temperature": 0.6, "max_tokens": 1200})

    def test_reasoning_effort_keeps_extra_body_and_uses_completion_token_limit(self):
        result = apply_reasoning_kwargs(
            {
                "model": "o3",
                "temperature": 0,
                "top_p": 1,
                "frequency_penalty": 0,
                "max_tokens": 1000,
                "extra_body": {"provider": {"order": ["fast"]}},
            },
            {"reasoning_effort": "high"},
        )

        self.assertNotIn("temperature", result)
        self.assertNotIn("top_p", result)
        self.assertNotIn("frequency_penalty", result)
        self.assertNotIn("max_tokens", result)
        self.assertEqual(result["max_completion_tokens"], 1000)
        self.assertEqual(result["extra_body"], {
            "provider": {"order": ["fast"]},
        })


class TranslationReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_config_reaches_the_api_body_and_switches_without_leaking(self):
        """Use the real SDK serializer, with HTTP intercepted before any network access."""
        bodies = []

        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0,
                "model": "custom-alias",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "translated",
                }}],
            })

        section = {
            "enableThinking": True, "thinkingType": "enabled", "reasoningEffort": "high",
            "tokens": [
                {"token": "sk-first", "modelName": "custom-alias"},
                {"token": "sk-second", "modelName": "custom-alias",
                 "enableThinking": False, "thinkingType": "disabled", "reasoningEffort": "none"},
            ],
        }
        pool = COpenAITokenPool(SimpleNamespace(getBackendConfigSection=lambda _: section), "ForGal")
        async with AsyncOpenAI(
            api_key="sk-test", base_url="https://example.test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            engine = SimpleNamespace(
                tokenStrategy="fallback", api_timeout=5, apiErrorWait=0,
                pj_config=SimpleNamespace(stop_event=None, active_workers=0, getProjectDir=lambda: ""),
                _is_stop_requested=lambda _: False,
                _coerce_positive_int=BaseTranslate._coerce_positive_int,
                _wait_for_global_rpm_slot=AsyncMock(), _record_request_health=Mock(),
            )
            for _, token in pool.tokens:
                engine.client_list = [(client, token)]
                result, _ = await BaseTranslate.ask_chatbot(
                    engine, prompt="translate", stream=False, temperature=0.6,
                    max_tokens=1200, max_retry_count=1,
                )
                self.assertEqual(result, "translated")

        first, second = bodies
        self.assertNotIn("enable_thinking", first)
        self.assertNotIn("thinking", first)
        self.assertEqual(first["reasoning_effort"], "high")
        self.assertEqual(first["max_completion_tokens"], 1200)
        self.assertNotIn("temperature", first)
        self.assertNotIn("enable_thinking", second)
        self.assertNotIn("thinking", second)
        self.assertEqual(second["reasoning_effort"], "none")
        self.assertEqual(second["temperature"], 0.6)


class AgentReasoningTests(unittest.TestCase):
    def test_main_child_and_summary_use_the_selected_backend_settings(self):
        profile = {"OpenAI-Compatible": {
            "enableThinking": True, "reasoningEffort": "medium",
            "tokens": [{"token": "sk-test", "modelName": "custom-alias", "reasoningEffort": "high"}],
        }}
        client = Mock()
        summary = "## 目标\nsummary\n## 已完成的工作\n无\n## 关键决策\n无\n## 当前进度与项目状态\n无\n## 待办与注意事项\n无"
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=summary, tool_calls=[])),
        ])
        with patch("openai.OpenAI", return_value=client):
            backend = resolve_llm_backend(profile)
            runner = AgentRunner(AgentState(project_dir="", session_id="", backend_profile_data=profile))
            runner._resolve_llm()

        runner.state.messages = [{"role": "user", "content": "start"}]
        runner._create_stream(include_usage=True)
        self.assertEqual(client.chat.completions.create.call_args.kwargs["reasoning_effort"], "high")
        self.assertNotIn("extra_body", client.chat.completions.create.call_args.kwargs)
        self.assertEqual(runner._summarize_messages(runner.state.messages), summary)
        self.assertNotIn("temperature", client.chat.completions.create.call_args.kwargs)

        child_client = Mock()
        child_client.chat.completions.create.return_value = client.chat.completions.create.return_value
        profile["OpenAI-Compatible"]["tokens"][0].update(enableThinking=False, reasoningEffort="low")
        with patch("openai.OpenAI", return_value=child_client):
            with runner._subagent_backend(profile, runner.stop_event) as view:
                child = SubAgentRunner(view, agent="explore", files=[], indexes="", brief="check", delegation_id="test")
                child.messages = [{"role": "user", "content": "check"}]
                child._chat_with_retry(view._openai_client, view._model, None)
                self.assertEqual(child_client.chat.completions.create.call_args.kwargs["reasoning_effort"], "low")
                self.assertNotIn("extra_body", child_client.chat.completions.create.call_args.kwargs)
                view._summarize_messages(child.messages)
                self.assertEqual(child_client.chat.completions.create.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(runner._reasoning_kwargs, backend.reasoning_kwargs)


if __name__ == "__main__":
    unittest.main()
