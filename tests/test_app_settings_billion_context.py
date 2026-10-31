"""Plugin settings are opt-in and partial HTTP saves preserve other preferences."""
import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from GalTransl.AppSettings import load_app_settings, save_app_settings


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", False),
    ("false", False), (1, False), (0, False), (None, False)])
@pytest.mark.parametrize("setting", ["billionContextEnabled", "extractionBillionContextEnabled"])
def test_only_explicit_boolean_enables_plugin(tmp_path, monkeypatch, value, expected, setting):
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert save_app_settings({setting: value})[setting] is expected
    assert load_app_settings()[setting] is expected


@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_switch_is_inherited_by_both_agents(tmp_path, monkeypatch, enabled):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(path))
    path.write_text(json.dumps({"billionContextEnabled": enabled}), encoding="utf-8")
    settings = load_app_settings()
    assert settings["billionContextEnabled"] is enabled
    assert settings["extractionBillionContextEnabled"] is enabled


def test_http_partial_settings_updates_preserve_plugin_and_other_preferences(tmp_path, monkeypatch):
    from GalTransl.server import JobRegistry, build_handler
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    save_app_settings({"maxConcurrentJobs": 7, "printTranslationLogInTerminal": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(JobRegistry()))
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    def update(patch):
        request = Request(f"http://127.0.0.1:{server.server_port}/api/app-settings",
            data=json.dumps(patch).encode(), method="PUT", headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)
    try:
        assert update({"billionContextEnabled": True}) == {
            "maxConcurrentJobs": 7, "printTranslationLogInTerminal": False,
            "billionContextEnabled": True, "extractionBillionContextEnabled": False}
        assert update({"extractionBillionContextEnabled": True})["billionContextEnabled"] is True
        assert update({"maxConcurrentJobs": 3})["billionContextEnabled"] is True
        assert update({"billionContextEnabled": False})["maxConcurrentJobs"] == 3
        assert load_app_settings()["billionContextEnabled"] is False
        assert load_app_settings()["extractionBillionContextEnabled"] is True
        assert update({"extractionBillionContextEnabled": False})["billionContextEnabled"] is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
