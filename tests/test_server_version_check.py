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
