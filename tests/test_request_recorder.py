"""录制模式：翻译请求按模板写进项目的 transl_record/<模板>.csv，「仅成功」只写整批解析通过的请求。

请求走真实的 BaseTranslate.ask_chatbot 和 OpenAI SDK，HTTP 在发出前被 MockTransport 截下，
所以 request 列核对的是 SDK 实际要发出的请求参数。
"""

import asyncio
import csv
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from GalTransl.AppSettings import load_app_settings, save_app_settings
from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
from GalTransl.Backend.Prompts import FORGAL_JSON_TRANS_PROMPT
from GalTransl.COpenAI import COpenAIToken
from GalTransl.CSentense import CSentense
from GalTransl.RequestRecorder import RECORD_COLUMNS, record_path


def _reply_for(body, broken):
    """按请求里本批的 sig|{json} 行生成回复；broken 时回一行解析不了的文字。"""
    if broken:
        return "sorry, cannot translate"
    # 解析失败后的重试会带上预置的 assistant 开头，本批输入在最后一条 user 消息里
    prompt = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
    rows = []
    for sig, obj in re.findall(r'([a-z0-9]{3})\|(\{"id".*\})', prompt.split("<input>")[-1]):
        obj = json.loads(obj)
        rows.append(sig + "|" + json.dumps({"id": obj["id"], "dst": "译-" + obj["src"]}, ensure_ascii=False))
    return "\n".join(rows)


def _engine(tmp_path, mode, client):
    engine = ForGalJsonTranslate.__new__(ForGalJsonTranslate)
    engine.eng_type = "ForGal-json"
    engine.pj_config = SimpleNamespace(
        active_workers=0, translation_guideline="", stop_event=None,
        runtime_project_dir=str(tmp_path), getProjectDir=lambda: str(tmp_path),
        bar=lambda *args: None, request_record_mode=mode,
    )
    engine.system_prompt = "system"
    engine.trans_prompt = FORGAL_JSON_TRANS_PROMPT
    engine.contextNum = 0
    engine.last_translations = {}
    engine.enhance_jailbreak = False
    engine.multi_turn = False
    engine.target_lang = "English"
    engine.source_lang = "Japanese"
    engine.smartRetry = False
    engine.max_api_retries = 1
    engine.api_timeout = 5
    engine.apiErrorWait = 0
    engine.global_request_rpm = 0
    engine.client_list = [(client, COpenAIToken("sk-test", "https://example.test/v1", "rec-model", False))]
    engine._interruptible_sleep = AsyncMock()
    engine._record_runtime_success = lambda *args: None
    return engine


async def _translate_with_one_parse_failure(tmp_path, mode):
    """第一次回复解析失败、重试成功，返回 CSV 里的行（没有文件时为 None）。"""
    replies = iter([True, False])

    def respond(request):
        body = json.loads(request.content)
        content = _reply_for(body, next(replies))
        return httpx.Response(200, json={
            "id": "t", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": content, "reasoning_content": "想一想",
            }}],
        })

    async with AsyncOpenAI(
        api_key="sk-test", base_url="https://example.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        engine = _engine(tmp_path, mode, client)
        items = [CSentense("こんにちは", "", 1), CSentense("さようなら", "", 2)]
        count, result = await engine.translate(items, "GLOSSARY-LINE", filename="scene.json")
    assert count == 2
    assert [item.pre_dst for item in result] == ["译-こんにちは", "译-さようなら"]
    path = record_path(str(tmp_path), "ForGal-json")
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            assert tuple(reader.fieldnames) == RECORD_COLUMNS
            return list(reader)
    except FileNotFoundError:
        return None


def test_success_mode_records_only_the_fully_parsed_request(tmp_path):
    rows = asyncio.run(_translate_with_one_parse_failure(tmp_path, "success"))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "success" and row["error"] == ""
    assert row["template"] == "ForGal-json" and row["model"] == "rec-model"
    assert row["file"] == "scene.json" and row["index_range"] == "1~2" and row["sentence_count"] == "2"
    assert "こんにちは" in row["input"] and "さようなら" in row["input"]
    assert row["gpt_dict"] == "GLOSSARY-LINE"
    assert row["context"] == ""
    request = json.loads(row["request"])
    assert request["model"] == "rec-model"
    assert "timeout" not in request
    assert request["messages"][0] == {"role": "system", "content": "system"}
    # 成功的是重试那次：请求末尾带着预置的回复开头，CSV 记的就是这次实际发出的请求
    assert request["messages"][-1] == {"role": "assistant", "content": "```jsonline"}
    assert "GLOSSARY-LINE" in request["messages"][1]["content"]
    assert "译-こんにちは" in row["response"]
    assert row["reasoning"] == "想一想"


def test_all_mode_also_records_failed_parses(tmp_path):
    rows = asyncio.run(_translate_with_one_parse_failure(tmp_path, "all"))
    assert [row["status"] for row in rows] == ["parse_error", "success"]
    assert rows[0]["response"] == "sorry, cannot translate"
    assert rows[0]["error"]


def test_off_mode_writes_nothing(tmp_path):
    assert asyncio.run(_translate_with_one_parse_failure(tmp_path, "off")) is None
    assert not (tmp_path / "transl_record").exists()


@pytest.mark.parametrize("value,expected", [("off", "off"), ("success", "success"), ("all", "all"),
    ("ALL", "off"), (True, "off"), (None, "off")])
def test_record_mode_setting_accepts_only_known_values(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(tmp_path))
    assert load_app_settings()["requestRecordMode"] == "off"
    assert save_app_settings({"requestRecordMode": value})["requestRecordMode"] == expected
    assert load_app_settings()["requestRecordMode"] == expected
