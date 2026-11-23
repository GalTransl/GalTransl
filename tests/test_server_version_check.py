import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import GalTransl.server as server
from GalTransl.server import JobRegistry, build_handler


def test_version_check_waits_for_the_startup_release_lookup(monkeypatch):
    # The desktop prewarms the backend, so its first version check can land
    # before the background GitHub lookup started at import time has finished.
    found: list[str] = []

    def slow_lookup():
        time.sleep(0.5)
        found.append("99.0.0")

    lookup = threading.Thread(target=slow_lookup)
    monkeypatch.setattr(server, "new_version", found)
    monkeypatch.setattr(server, "update_thread", lookup)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
    serving = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    serving.start()
    lookup.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/version/check"
        with urllib.request.urlopen(url, timeout=15) as response:
            result = json.load(response)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert result["latest_version"] == "99.0.0"
    assert result["update_available"] is True


def _serve(routes):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = json.dumps(routes[self.path]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return httpd


def test_version_check_returns_the_summary_paragraph_of_the_latest_release(monkeypatch):
    # The update dialog shows the paragraph directly under the release title,
    # so the startup lookup has to carry it from the release body to the API.
    import application_update
    from GalTransl.Utils import check_for_tool_updates

    body = "# GalTransl 99.0.0\r\n\r\n本次更新调整了默认批次大小，\r\n工作台新增 Token 用量统计。\r\n\r\n## 🌟 主要更新\r\n\r\n### 1. 默认每次请求 32 句\r\n"
    github = _serve({"/latest": {"tag_name": "99.0.0", "body": body}})
    found: list[str] = []
    summary: list[str] = []
    monkeypatch.setattr(application_update, "RELEASE_API", f"http://127.0.0.1:{github.server_address[1]}/latest")
    lookup = threading.Thread(target=check_for_tool_updates, args=(found, summary))
    monkeypatch.setattr(server, "new_version", found)
    monkeypatch.setattr(server, "release_summary", summary)
    monkeypatch.setattr(server, "update_thread", lookup)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    lookup.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/version/check"
        with urllib.request.urlopen(url, timeout=15) as response:
            result = json.load(response)
    finally:
        for running in (httpd, github):
            running.shutdown()
            running.server_close()
    assert result["latest_version"] == "99.0.0"
    assert result["release_summary"] == "本次更新调整了默认批次大小，\n工作台新增 Token 用量统计。"


def test_release_summary_is_empty_when_the_release_has_no_leading_paragraph():
    # Auto-generated notes open with a heading followed by a list; a list
    # item is not a summary, and a missing body must not break the lookup.
    from GalTransl.Utils import extract_release_summary

    assert extract_release_summary("## What's Changed\n* fix a by @x in #1\n* fix b\n") == ""
    assert extract_release_summary("1. first\n2. second") == ""
    assert extract_release_summary(None) == ""
    assert extract_release_summary("") == ""
    assert extract_release_summary("Plain first paragraph.\n\nSecond paragraph.") == "Plain first paragraph."
