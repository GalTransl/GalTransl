from __future__ import annotations

import ipaddress
import json
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

from .environment import environment, save_settings
from .runtime import EXTRACTION_RUNTIME


def trusted_request(handler) -> bool:
    """Keep a website from using the local coding-agent endpoint."""
    try:
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback:
            return False
    except ValueError:
        return False
    origin = handler.headers.get("Origin", "")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1", "tauri.localhost"} and parsed.scheme in {"http", "https", "tauri"}


def stream_events(handler, session_id: str, after: int) -> None:
    EXTRACTION_RUNTIME.session(session_id)
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    heartbeat = time.monotonic()
    try:
        while True:
            state = EXTRACTION_RUNTIME.poll(session_id, after)
            if state["reset"]:
                message = {"type": "reset", "snapshot": EXTRACTION_RUNTIME.snapshot(session_id)}
                handler.wfile.write(("data: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode("utf-8"))
            else:
                for event in state["events"]:
                    handler.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
            after = state["cursor"]
            if not state["running"]:
                handler.wfile.write(b'data: {"type":"close"}\n\n')
                handler.wfile.flush()
                return
            if time.monotonic() - heartbeat >= 10:
                handler.wfile.write(b": heartbeat\n\n")
                heartbeat = time.monotonic()
            handler.wfile.flush()
            time.sleep(0.1)
    except (BrokenPipeError, ConnectionResetError):
        return


def route_extraction(handler, method: str) -> bool:
    parsed = urlparse(handler.path)
    if not parsed.path.startswith("/api/extraction/"):
        return False
    if not trusted_request(handler):
        handler._send_json({"error": "Extraction is only available to the local desktop client"}, status=HTTPStatus.FORBIDDEN)
        return True
    action = parsed.path.removeprefix("/api/extraction/")
    query = parse_qs(parsed.query)
    session_id = query.get("session_id", [""])[0]
    try:
        if method == "GET":
            if action == "environment":
                result = environment()
            elif action == "sessions":
                result = EXTRACTION_RUNTIME.list_sessions(int(query.get("offset", ["0"])[0]))
            elif action == "session":
                before = query.get("before_seq", [None])[0]
                result = EXTRACTION_RUNTIME.snapshot(session_id, int(before) if before else None)
            elif action == "files":
                result = EXTRACTION_RUNTIME.store.files(session_id, query.get("path", [""])[0],
                                                        int(query.get("offset", ["0"])[0]))
            elif action == "preview":
                result = EXTRACTION_RUNTIME.store.preview(session_id, query.get("path", [""])[0],
                                                          int(query.get("offset", ["0"])[0]))
            elif action == "stream":
                stream_events(handler, session_id, max(0, int(query.get("after", ["0"])[0])))
                return True
            else:
                handler._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return True
        elif method == "POST":
            content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip()
            length = int(handler.headers.get("Content-Length", "0"))
            maximum = 12 * 1024 * 1024 if action == 'message' else 1024 * 1024
            if content_type != "application/json" or length <= 0 or length > maximum:
                raise ValueError('JSON request exceeds its size budget')
            payload = handler._read_json_body()
            session_id = str(payload.get("session_id", ""))
            if action == "settings":
                save_settings(payload)
                result = environment()
            elif action == "create":
                result = {"session": EXTRACTION_RUNTIME.store.create(str(payload.get("game_dir", "")),
                                                                     str(payload.get("output_dir", "")))}
            elif action == "message":
                result = EXTRACTION_RUNTIME.start(session_id, payload)
            elif action == "answer":
                result = EXTRACTION_RUNTIME.answer(session_id, payload)
            elif action == "permission-mode":
                result = EXTRACTION_RUNTIME.set_permission_mode(session_id, payload.get('permission_mode'))
            elif action == "stop":
                result = EXTRACTION_RUNTIME.stop(session_id)
            elif action == "import":
                result = EXTRACTION_RUNTIME.import_project(session_id)
            else:
                handler._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return True
        else:
            return False
        handler._send_json(result)
    except FileNotFoundError as exc:
        handler._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
    except (ValueError, TypeError, FileExistsError, NotADirectoryError) as exc:
        handler._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
    except Exception:
        handler._send_json({"error": "Extraction operation failed; check paths and file permissions"},
                           status=HTTPStatus.INTERNAL_SERVER_ERROR)
    return True
