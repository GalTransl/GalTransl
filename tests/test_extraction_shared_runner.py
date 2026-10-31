"""Isolated local model/tool integration: the extraction agent uses AgentRunner."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from GalTransl.Agent.core import AgentStopRequested
from GalTransl.Agent.models import AgentToolError
from GalTransl.Agent.runner import AgentRunner
from GalTransl.Extraction import runtime as module
from GalTransl.Extraction.environment import builtin_python, python_runner_script
from GalTransl.Extraction.runner import ExtractionRunner, restore_state
from GalTransl.Extraction.store import SessionStore
from GalTransl.Extraction.tool_schemas import EXTRACTION_TOOLS, validate_arguments
from GalTransl.Extraction.tools import ExtractionTools, checked_path


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    pytest.fail("Timed out waiting for extraction state")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Ordinary-mode contracts must not depend on the developer's plugin setting.
    monkeypatch.setattr("GalTransl.Agent.runner.load_app_settings", lambda: {"billionContextEnabled": False})
    game, skill = tmp_path / "game", tmp_path / "skill"
    game.mkdir()
    skill.mkdir()
    (skill / "SKILL.md").write_text("Synthetic isolated extraction Skill", encoding="utf-8")
    available = {"ready": True, "problems": [], "settings": {"skill_dir": str(skill), "python_path": ""},
                 "builtin_python": builtin_python(), "python_runner_script": python_runner_script(), "system_pythons": []}
    monkeypatch.setattr(module, "environment", lambda: available)
    store = SessionStore(tmp_path / "sessions")
    session = store.create(str(game))
    runtime = module.ExtractionRuntime(store)
    yield runtime, session, available
    runtime.close()


@pytest.fixture
def model_server():
    requests, responses, served = [], [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(raw)
            # Windows loopback occasionally resets a finished stream; replay the scripted answer for the client's identical retry.
            payload = served.get(raw)
            if payload is not None:
                self._stream(payload)
                return
            requests.append(request)
            pending = set()
            for message in request['messages']:
                if message['role'] == 'tool':
                    content = message.get('content')
                    if not (isinstance(content, str) or isinstance(content, list)
                            and all(block.get('type') == 'text' for block in content)):
                        self.send_error(400, 'Tool messages accept text only')
                        return
                    if message.get('tool_call_id') not in pending:
                        self.send_error(400, 'Unexpected tool response')
                        return
                    pending.remove(message['tool_call_id'])
                else:
                    if pending:
                        self.send_error(400, 'Missing tool responses before next message')
                        return
                    pending = {tool['id'] for tool in message.get('tool_calls', [])}
            if pending:
                self.send_error(400, 'Missing tool responses')
                return
            if not responses:
                self.send_error(400, "Unexpected synthetic model request")
                return
            payload = served[raw] = responses.pop(0)
            self._stream(payload)

        def _stream(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "synthetic", "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": {"role": "assistant", **payload},
                     "finish_reason": "tool_calls" if payload.get("tool_calls") else "stop"}]},
                {"id": "synthetic", "object": "chat.completion.chunk", "choices": [],
                 "usage": {"prompt_tokens": 3000, "completion_tokens": 40, "total_tokens": 3040,
                           "prompt_tokens_details": {"cached_tokens": 2000}}},
            ]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    profile = {"OpenAI-Compatible": {"tokens": [{"token": "synthetic-key", "modelName": "synthetic",
                "endpoint": f"http://127.0.0.1:{server.server_port}/v1", "contextWindow": 128000,
                "reasoningEffort": "none"}]}}
    yield profile, requests, responses
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


def call(name, args, ident="synthetic-call"):
    return {"index": 0, "id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_shared_loop_real_tool_approval_usage_and_restart(setup, model_server):
    runtime, session, available = setup
    profile, requests, responses = model_server
    sid, workspace = session["id"], Path(session["workspace"])
    reason = '验证提取脚本并保存检查结果'
    responses.extend([
        {"reasoning_content": "Inspect only synthetic files.", "content": "Checking the synthetic output.",
         "tool_calls": [call("run_python", {"code": "from pathlib import Path; Path('reports/fixture.txt').write_text('synthetic'); print('checked synthetic')", "reason": reason})]},
        {"content": "Synthetic check finished."},
    ])
    runtime.start(sid, {"message": "Check synthetic fixture", "profile": profile, "permission_mode": "ask"})
    pending = wait_for(lambda: runtime.snapshot(sid)["pending"])
    assert pending["toolName"] == "run_python"
    assert pending['arguments']['reason'] == reason
    assert not (workspace / "reports/fixture.txt").exists()
    with pytest.raises(ValueError, match="already running"):
        runtime.start(sid, {"message": "duplicate", "profile": profile})
    runtime.answer(sid, {"request_id": pending["requestId"], "decision": "allow-once"})
    wait_for(lambda: sid not in runtime.runs)
    assert runtime.session(sid)["status"] == "idle"
    assert (workspace / "reports/fixture.txt").read_text() == "synthetic"
    assert any(message.get("role") == "tool" and "checked synthetic" in message["content"] for message in requests[1]["messages"])
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {tool["function"]["name"] for tool in EXTRACTION_TOOLS}
    assert "start_translation" not in str(requests[0]["tools"])
    assert requests[0]["reasoning_effort"] == "none"
    first_user_message = next(message['content'] for message in requests[0]['messages'] if message['role'] == 'user')
    assert '在第一次工具调用之前' in first_user_message
    assert '主要步骤及验证方式' in first_user_message
    assert '在连续工具调用之间穿插简短的助手正文反馈' in first_user_message
    assert '不能仅放在思考内容或工具的 reason 参数中' in first_user_message
    assert first_user_message.endswith('用户原始消息：\nCheck synthetic fixture')
    events = runtime.snapshot(sid)["events"]
    intent_events = [event for event in events if event['type'] in {'tool_start', 'permission_request'}]
    assert len(intent_events) == 2
    assert all(event['arguments']['reason'] == reason for event in intent_events)
    user_events = [event for event in events if event["type"] == "user_message"]
    assert [event["text"] for event in user_events] == ["Check synthetic fixture"]
    assert user_events[0]["seq"] < next(event["seq"] for event in events if event["type"] == "assistant_start")
    assert [event["text"] for event in runtime.poll(sid, 0)["events"] if event["type"] == "user_message"] == ["Check synthetic fixture"]
    assert any(event["type"] == "thinking_message" and "synthetic" in event["text"] for event in events)
    usage = [event for event in events if event["type"] == "context_usage"][-1]
    assert usage["used_tokens"] >= 3000 and usage["window_tokens"] == 128000
    assert usage["cache"]["cached_tokens"] > 0
    assert all("synthetic-key" not in path.read_text(encoding="utf-8") for path in runtime.store.directory(sid).rglob("*.json*"))

    # A new runtime must load the shared model journal, not just replay UI bubbles.
    restored = module.ExtractionRuntime(runtime.store)
    responses.append({"content": "Continuing the same synthetic conversation."})
    restored.start(sid, {"message": "Continue", "profile": profile})
    wait_for(lambda: sid not in restored.runs)
    assert any(message.get("content") == "Synthetic check finished." for message in requests[-1]["messages"])
    assert requests[-1]["messages"][0] == requests[0]["messages"][0]
    assert requests[-1]["messages"][-1]["content"] == "Continue"
    assert next(message['content'] for message in requests[-1]['messages'] if message['role'] == 'user') == first_user_message
    replay = module.ExtractionRuntime(runtime.store).snapshot(sid)["events"]
    assert [event for event in replay if event['type'] in {'tool_start', 'permission_request'}] == intent_events
    assert [event["text"] for event in replay if event["type"] == "user_message"] == ["Check synthetic fixture", "Continue"]
    continuation = next(index for index, event in enumerate(replay) if event.get("text") == "Continue")
    assert replay[continuation + 1]["type"] == "state"
    assert replay[continuation + 1]["status"] == "running"
    restored.close()


@pytest.mark.parametrize("decision", ["deny", "stop"])
def test_denied_or_stopped_command_is_not_executed(setup, model_server, decision):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    responses.append({"tool_calls": [call("run_python", {"code": "open('must-not-exist', 'w').write('synthetic')"})]})
    if decision == "deny":
        responses.append({"content": "The denied operation was not performed."})
    sid = session["id"]
    runtime.start(sid, {"message": "Synthetic permission test", "profile": profile, "permission_mode": "ask"})
    pending = wait_for(lambda: runtime.snapshot(sid)["pending"])
    if decision == "stop":
        runtime.stop(sid)
    else:
        runtime.answer(sid, {"request_id": pending["requestId"], "decision": decision})
    wait_for(lambda: sid not in runtime.runs)
    assert not (Path(session["workspace"]) / "must-not-exist").exists()
    assert runtime.snapshot(sid)["pending"] is None
    assert any(event["type"] == "tool_end" and event["isError"] for event in runtime.snapshot(sid)["events"])
    assert runtime.session(sid)["status"] == ("stopped" if decision == "stop" else "idle")
    if decision == "deny":
        assert "拒绝" in requests[-1]["messages"][-1]["content"]


def test_shared_ask_wait_is_answerable_and_replays(setup, model_server):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    responses.extend([{"tool_calls": [call("ask_user", {"questions": [
        {"question": "Which synthetic output?", "options": ["fixture A", "fixture B"], "recommended": "fixture B"}
    ]})]}, {"content": "Answer received."}])
    sid = session["id"]
    runtime.start(sid, {"message": "Ask a synthetic question", "profile": profile})
    pending = wait_for(lambda: runtime.snapshot(sid)["pending"])
    assert pending["type"] == "ask_request"
    runtime.answer(sid, {"request_id": pending["requestId"], "text": "fixture A"})
    wait_for(lambda: sid not in runtime.runs)
    assert json.loads(requests[-1]["messages"][-1]["content"])["answers"] == [["fixture A"]]
    assert runtime.session(sid)["status"] == "idle"


@pytest.mark.parametrize('mode', [None, 'auto', 'auto-quiet'])
def test_automatic_modes_execute_tools_and_only_quiet_mode_answers_questions(setup, model_server, mode):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    questions = [
        {'question': 'Choose output', 'options': ['first', 'recommended'], 'recommended': 'recommended'},
        {'question': 'Choose fallback', 'options': ['fallback', 'other']},
    ]
    responses.extend([{'tool_calls': [
        call('write', {'path': 'scripts/automatic.txt', 'content': 'executed'}, 'write'),
        {**call('ask_user', {'questions': questions}, 'question'), 'index': 1},
    ]}, {'content': 'Completed.'}])
    sid = session['id']
    payload = {'message': 'Automatic mode fixture', 'profile': profile}
    if mode is not None:
        payload['permission_mode'] = mode
    runtime.start(sid, payload)
    wait_for(lambda: runtime.snapshot(sid)['pending'] or sid not in runtime.runs)
    assert (Path(session['workspace']) / 'scripts/automatic.txt').read_text() == 'executed'
    pending = runtime.snapshot(sid)['pending']
    if mode != 'auto-quiet':
        assert pending['type'] == 'ask_request'
        assert pending['questions'] == [{**question, 'multiSelect': False, 'recommended': question.get('recommended', '')}
                                        for question in questions]
        runtime.answer(sid, {'request_id': pending['requestId'], 'answers': [['first'], ['other']]})
    else:
        assert pending is None
    wait_for(lambda: sid not in runtime.runs)
    assert runtime.session(sid)['status'] == 'idle'
    assert runtime.session(sid)['permission_mode'] == (mode or 'auto')
    answer = json.loads(requests[-1]['messages'][-1]['content'])
    assert answer['answers'] == ([['recommended'], ['fallback']] if mode == 'auto-quiet' else [['first'], ['other']])
    assert bool(answer.get('auto_answered')) == (mode == 'auto-quiet')
    assert not any(event['type'] == 'permission_request' for event in runtime.snapshot(sid)['events'])
    if mode == 'auto-quiet':
        assert not any(event['type'] == 'ask_request' for event in runtime.snapshot(sid)['events'])


def test_live_mode_changes_release_approval_and_answer_pending_question_by_recommendation(setup, model_server):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    responses.extend([
        {'tool_calls': [call('write', {'path': 'scripts/live.txt', 'content': 'approved'}, 'write')]},
        {'tool_calls': [call('ask_user', {'questions': [
            {'question': 'Choose', 'options': ['first', 'recommended'], 'recommended': 'recommended'}
        ]}, 'question')]},
        {'tool_calls': [call('ask_user', {'questions': [{'question': 'Fallback', 'options': ['fallback', 'other']}]}, 'fallback')]},
        {'content': 'Completed after changing modes.'},
    ])
    sid = session['id']
    runtime.start(sid, {'message': 'Live mode fixture', 'profile': profile, 'permission_mode': 'ask'})
    wait_for(lambda: runtime.snapshot(sid)['pending'])
    runner = runtime.runs[sid]
    runner.state.permission_grants.add('run_python')
    assert runtime.set_permission_mode(sid, 'auto')['permission_mode'] == 'auto'
    assert runner.state.permission_grants == set()
    pending = wait_for(lambda: (value if (value := runtime.snapshot(sid)['pending']) and value['type'] == 'ask_request' else None))
    assert (Path(session['workspace']) / 'scripts/live.txt').read_text() == 'approved'
    assert pending['questions'][0]['recommended'] == 'recommended'
    runtime.set_permission_mode(sid, 'auto-quiet')
    wait_for(lambda: sid not in runtime.runs)
    assert runtime.session(sid)['status'] == 'idle'
    assert runtime.session(sid)['permission_mode'] == 'auto-quiet'
    results = {message['tool_call_id']: json.loads(message['content']) for message in requests[-1]['messages']
               if message['role'] == 'tool' and message['tool_call_id'] in {'question', 'fallback'}}
    assert results['question']['answers'] == [['recommended']]
    assert results['fallback']['answers'] == [['fallback']]
    events = runtime.snapshot(sid)['events']
    assert len([event for event in events if event['type'] == 'ask_request']) == 1
    assert [event['permission_mode'] for event in events if event['type'] == 'permission_mode'] == ['auto', 'auto-quiet']
    assert runtime.snapshot(sid)['pending'] is None
    runtime.set_permission_mode(sid, 'ask')
    assert module.ExtractionRuntime(runtime.store).snapshot(sid)['session']['permission_mode'] == 'ask'
    with pytest.raises(ValueError, match='Invalid permission'):
        runtime.set_permission_mode(sid, 'invalid')


def test_live_switch_to_stricter_mode_requires_approval_for_next_tool(setup, model_server):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    responses.extend([
        {'tool_calls': [call('ask_user', {'questions': [{'question': 'Choose', 'options': ['continue', 'stop']}]}, 'question')]},
        {'tool_calls': [call('write', {'path': 'scripts/denied.txt', 'content': 'must not write'}, 'write')]},
        {'content': 'The file was not written.'},
    ])
    sid = session['id']
    runtime.start(sid, {'message': 'Stricter mode fixture', 'profile': profile})
    pending = wait_for(lambda: runtime.snapshot(sid)['pending'])
    runtime.set_permission_mode(sid, 'ask')
    assert runtime.snapshot(sid)['pending']['requestId'] == pending['requestId']
    runtime.answer(sid, {'request_id': pending['requestId'], 'answers': [['continue']]})
    approval = wait_for(lambda: runtime.snapshot(sid)['pending'])
    assert approval['type'] == 'permission_request'
    runtime.answer(sid, {'request_id': approval['requestId'], 'decision': 'deny'})
    wait_for(lambda: sid not in runtime.runs)
    assert runtime.session(sid)['status'] == 'idle'
    assert not (Path(session['workspace']) / 'scripts/denied.txt').exists()


def test_multiple_tool_calls_wait_for_separate_approvals_in_order(setup, model_server):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    responses.extend([{'tool_calls': [
        call('write', {'path': 'scripts/one.txt', 'content': 'first'}, 'one'),
        {**call('write', {'path': 'scripts/two.txt', 'content': 'second'}, 'two'), 'index': 1},
    ]}, {'content': 'Only the approved file was written.'}])
    sid, workspace = session['id'], Path(session['workspace'])
    runtime.start(sid, {'message': 'Synthetic sequential approvals', 'profile': profile, 'permission_mode': 'ask'})
    first = wait_for(lambda: runtime.snapshot(sid)['pending'])
    assert first['toolCallId'] == 'one'
    assert not list((workspace / 'scripts').iterdir())
    assert len([event for event in runtime.snapshot(sid)['events'] if event['type'] == 'permission_request']) == 1
    runtime.answer(sid, {'request_id': first['requestId'], 'decision': 'allow-once'})
    second = wait_for(lambda: (pending if (pending := runtime.snapshot(sid)['pending'])
                               and pending['requestId'] != first['requestId'] else None))
    assert second['toolCallId'] == 'two'
    assert (workspace / 'scripts/one.txt').read_text() == 'first'
    runtime.answer(sid, {'request_id': second['requestId'], 'decision': 'deny'})
    wait_for(lambda: sid not in runtime.runs)
    assert not (workspace / 'scripts/two.txt').exists()
    assert [event['toolCallId'] for event in runtime.snapshot(sid)['events']
            if event['type'] == 'tool_end'] == ['one', 'two']


def test_native_image_and_shell_error_survive_model_request_and_replay(setup, model_server):
    from PIL import Image
    from GalTransl.Agent.session_store import SessionStore as SharedStore
    runtime, session, _ = setup
    profile, requests, responses = model_server
    workspace = Path(session['workspace'])
    Image.new('RGB', (32, 16), 'red').save(workspace / 'picture.gif', 'GIF')
    responses.extend([
        {'tool_calls': [
            call('read', {'path': 'picture.gif'}, 'picture'),
            {**call('bash', {'command': 'printf failed; exit 9'}, 'nonzero'), 'index': 1},
            {**call('read', {'path': 'picture.gif'}, 'picture-again'), 'index': 2},
        ]},
        {'content': 'The image was attached; command failed as expected.'},
    ])
    sid = session['id']
    runtime.start(sid, {'message': 'Synthetic image and failure', 'profile': profile, 'permission_mode': 'auto'})
    pending = wait_for(lambda: runtime.snapshot(sid)['pending'] or (sid not in runtime.runs))
    if isinstance(pending, dict):
        runtime.answer(sid, {'request_id': pending['requestId'], 'decision': 'allow-once'})
    wait_for(lambda: sid not in runtime.runs)
    assert runtime.session(sid)['status'] == 'idle'
    tool_messages = [message for message in requests[-1]['messages'] if message['role'] == 'tool']
    assert len(tool_messages) == 3 and all(isinstance(message['content'], str) for message in tool_messages)
    assert tool_messages[0]['content'] == 'Read image file [image/gif]'
    assert tool_messages[1]['content'] == 'failed\n\nCommand exited with code 9'
    messages = requests[-1]['messages']
    start = next(index for index, message in enumerate(messages) if message.get('tool_call_id') == 'picture')
    assert [message['role'] for message in messages[start:]] == ['tool', 'tool', 'tool', 'user', 'user']
    for message, ident in zip(messages[-2:], ('picture', 'picture-again')):
        assert message['content'][0]['text'] == f'Images returned by tool call {ident}:'
        assert message['content'][1]['image_url']['url'].startswith('data:image/gif;base64,')
    events = runtime.snapshot(sid)['events']
    native = [event for event in events if event['type'] == 'tool_end']
    assert native[0]['images'][0]['mime_type'] == 'image/gif'
    assert native[1]['isError'] is True
    directory = runtime.store.directory(sid)
    assert 'base64' not in (directory / 'events.jsonl').read_text(encoding='utf-8')
    shared = SharedStore(session['workspace'], sid, directory=str(directory / 'execution'))
    assert all('base64' not in json.dumps(message) for message in shared.load()['messages'])
    assert module.ExtractionRuntime(runtime.store).snapshot(sid)['events'] == events
    responses.append({'content': 'Restored the same image attachments.'})
    restored = module.ExtractionRuntime(runtime.store)
    try:
        restored.start(sid, {'message': 'Continue', 'profile': profile})
        wait_for(lambda: sid not in restored.runs)
        assert restored.session(sid)['status'] == 'idle'
        assert requests[-1]['messages'][:len(messages)] == messages
        assert [event['text'] for event in restored.snapshot(sid)['events'] if event['type'] == 'user_message'] == [
            'Synthetic image and failure', 'Continue',
        ]
    finally:
        restored.close()


def test_native_large_write_previews_do_not_break_ui_journal(setup, model_server):
    runtime, session, _ = setup
    profile, requests, responses = model_server
    content = '中' * 100000
    responses.extend([{'tool_calls': [call('write', {'path': 'scripts/large.txt', 'content': content})]}, {'content': 'Written.'}])
    sid = session['id']
    runtime.start(sid, {'message': 'Synthetic large write', 'profile': profile, 'permission_mode': 'ask'})
    pending = wait_for(lambda: runtime.snapshot(sid)['pending'])
    assert len(json.dumps(pending, ensure_ascii=False).encode()) < 256 * 1024
    assert '[preview truncated]' in pending['arguments']['content']
    runtime.answer(sid, {'request_id': pending['requestId'], 'decision': 'allow-once'})
    wait_for(lambda: sid not in runtime.runs)
    assert (Path(session['workspace']) / 'scripts/large.txt').read_text(encoding='utf-8') == content
    assert runtime.session(sid)['status'] == 'idle'


def make_tools(setup):
    _, session, available = setup
    events, stop = [], threading.Event()
    return ExtractionTools(session["workspace"], session["game_dir"], available["settings"]["skill_dir"], available, stop, events.append), events, stop


def test_file_tools_edit_and_source_boundaries(setup, tmp_path):
    tools, _, _ = make_tools(setup)
    root = tools.workspace
    tools.write({"path": "scripts/fixture.txt", "content": "one\r\ntwo\r\n"}, "write")
    tools.edit({"path": "scripts/fixture.txt", "edits": [{"oldText": "two", "newText": "二"}]}, "edit")
    assert (root / "scripts/fixture.txt").read_bytes() == "one\r\n二\r\n".encode()
    assert 'offset=2' in tools.read({"path": "scripts/fixture.txt", "limit": 1}, "read")["content"][0]["text"]
    with pytest.raises(AgentToolError, match="Could not find"):
        tools.edit({"path": "scripts/fixture.txt", "edits": [{"oldText": "missing", "newText": "x"}]}, "edit")
    for path in (str(tools.game / "source.txt"), "../outside.txt", str(tmp_path / "outside.txt")):
        with pytest.raises(AgentToolError):
            tools.write({"path": path, "content": "must not write"}, "write")
    with pytest.raises(AgentToolError):
        checked_path(str(tmp_path), root, [root])


def test_commands_log_fail_timeout_and_cancellation(setup):
    tools, events, stop = make_tools(setup)
    result = tools.run_python({"code": "print('synthetic 中文')"}, "python")
    assert result["exit_code"] == 0 and "synthetic 中文" in result["output"]
    assert (tools.workspace / result["log_path"]).is_file()
    assert any(event["type"] == "tool_output" for event in events)
    shell = tools.bash({"command": "printf 'synthetic shell'"}, "shell")
    assert "synthetic shell" in shell["content"][0]["text"]
    with pytest.raises(AgentToolError, match='"exit_code": 7'):
        tools.run_python({"code": "raise SystemExit(7)"}, "failure")
    with pytest.raises(AgentToolError, match="time limit"):
        tools.run_python({"code": "import time; time.sleep(30)", "timeout_seconds": 1}, "timeout")
    with pytest.raises(AgentToolError, match="time limit"):
        tools.run_python({"code": "import os, time; os.close(1); os.close(2); time.sleep(30)", "timeout_seconds": 1}, "closed-output")
    timer = threading.Timer(.4, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(AgentStopRequested):
            tools.run_python({"code": "import time; time.sleep(30)"}, "cancel")
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5


def test_stopping_a_command_terminates_its_child_processes(setup):
    tools, events, stop = make_tools(setup)
    errors = []
    code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; time.sleep(2); Path('must-not-survive').touch()\"]); print('spawned', flush=True); time.sleep(30)"

    def execute():
        try:
            tools.run_python({"code": code}, "process-tree")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    wait_for(lambda: any("spawned" in event.get("text", "") for event in events))
    stop.set()
    tools.commands.stop()
    worker.join(timeout=4)
    assert not worker.is_alive() and isinstance(errors[0], AgentStopRequested)
    time.sleep(2.1)
    assert not (tools.workspace / "must-not-survive").exists()


def test_command_output_is_bounded_and_complete_log_is_available(setup):
    tools, _, _ = make_tools(setup)
    result = tools.run_python({"code": "print('synthetic ' * 5000)"}, "preview")
    assert len(result["output"]) <= 16000 and result["truncated"]
    assert (tools.workspace / result["log_path"]).stat().st_size == result["output_bytes"]
    with pytest.raises(AgentToolError, match="output budget"):
        tools.run_python({"code": "import sys; sys.stdout.write('x' * (11 * 1024 * 1024))"}, "budget")
    assert all(path.stat().st_size <= 10 * 1024 * 1024 for path in (tools.workspace / "reports").glob("*.log"))


def test_legacy_development_history_import_does_not_need_node(setup):
    runtime, session, _ = setup
    from GalTransl.Extraction.legacy import previous_messages
    directory = runtime.store.directory(session["id"])
    legacy = directory / "old-session.jsonl"
    records = [
        {"type": "message", "id": "one", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": "old synthetic question"}]}},
        {"type": "message", "id": "two", "parentId": "one", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call", "name": "read_file", "arguments": {"path": "SKILL.md"}}]}},
        {"type": "message", "id": "three", "parentId": "two", "message": {"role": "toolResult", "toolCallId": "call", "content": [{"type": "text", "text": "synthetic result"}]}},
        {"type": "message", "id": "abandoned", "parentId": "three", "message": {"role": "assistant", "content": "discarded branch"}},
        {"type": "message", "id": "four", "parentId": "three", "message": {"role": "assistant", "content": "selected branch"}},
    ]
    legacy.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    messages = previous_messages({**session, "pi_session_file": str(legacy)}, directory)
    assert len(messages) == 4 and messages[-1]["content"] == "selected branch"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert messages[2]["tool_call_id"] == "call"
    assert all("discarded branch" not in str(message) for message in messages)
    with pytest.raises(ValueError, match="outside"):
        previous_messages({**session, "pi_session_file": str(legacy)}, directory / "wrong-root")


def test_resumed_extraction_refreshes_wizard_instructions_without_rewriting_history(setup):
    runtime, session, available = setup
    state, store = restore_state(session, runtime.store.directory(session["id"]), {}, "continue", "ask")
    runner = ExtractionRunner(state, store, session, available, lambda event: None)
    old = {"role": "system", "content": "Old generated extraction instructions"}
    runner._persist_message(old)
    runner._persist_message({"role": "user", "content": "Finish extracting"})
    prompt = runner._messages_for_request()[0]["content"]
    assert '@new_project_wizard("absolute project directory")' in prompt
    assert "CONTAINS gt_input" in prompt
    assert "Do not create config.yaml yourself" in prompt
    assert str(session["workspace"]) in prompt
    assert state.messages[0] == old
    assert store.load()["messages"][0] == old


def test_extraction_compaction_keeps_own_prompt_tools_and_archives(setup):
    runtime, session, available = setup
    state, store = restore_state(session, runtime.store.directory(session["id"]), {}, "synthetic", "ask")
    runner = ExtractionRunner(state, store, session, available, lambda event: None)
    messages = [{"role": "system", "content": runner._system_prompt()}, {"role": "user", "content": "old synthetic task"},
                {"role": "assistant", "content": "old response"}, {"role": "user", "content": "keep this new task"}]
    for message in messages:
        runner._persist_message(message)
    runner._pending_compaction = {"cut": 3, "estimated": 12000}
    assert runner._rebuild_after_compaction("Synthetic summary", "synthetic")
    assert runner.state.messages[0] == messages[0]
    assert runner.state.messages[-1] == messages[-1]
    assert runner._dispatch_tool("read_history_archive", {})["archives"]
    restored, _ = restore_state(session, runtime.store.directory(session["id"]), {}, "continue", "ask")
    assert restored.messages == runner.state.messages
    assert ExtractionRunner.run is AgentRunner.run
    assert ExtractionRunner._begin_compaction is AgentRunner._begin_compaction
    assert ExtractionRunner.request_permission is AgentRunner.request_permission
    assert len(runner._tool_definitions()) == 7
    with pytest.raises(AgentToolError):
        validate_arguments("run_python", {"code": "pass", "timeout_seconds": True})
