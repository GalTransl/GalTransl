"""billion-context defaults off, agent-mnemon defaults on, and partial HTTP saves preserve other preferences."""
import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from GalTransl.AppSettings import load_app_settings, save_app_settings


@pytest.mark.parametrize("source", [None, {}, {"maxConcurrentJobs": 7}])
def test_plugin_defaults_for_new_and_legacy_settings(tmp_path, monkeypatch, source):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(path))
    # 没有设置文件时会回退读资源目录里的旧版 app_settings.json；指到空目录，免得读到开发机上的那份
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(tmp_path))
    if source is not None:
        path.write_text(json.dumps(source), encoding="utf-8")
    settings = load_app_settings()
    assert settings["billionContextEnabled"] is False
    assert settings["extractionBillionContextEnabled"] is False
    assert settings["mnemonEnabled"] is True
    assert settings["extractionMnemonEnabled"] is True
    assert save_app_settings(settings) == settings
    assert load_app_settings() == settings


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", False),
    ("false", False), (1, False), (0, False), (None, False)])
@pytest.mark.parametrize("setting", ["billionContextEnabled", "extractionBillionContextEnabled", "mnemonEnabled", "extractionMnemonEnabled"])
def test_only_explicit_boolean_enables_plugin(tmp_path, monkeypatch, value, expected, setting):
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert save_app_settings({setting: value})[setting] is expected
    assert load_app_settings()[setting] is expected


@pytest.mark.parametrize("enabled", [True, False])
def test_single_switch_is_inherited_by_both_agents(tmp_path, monkeypatch, enabled):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(path))
    path.write_text(json.dumps({"billionContextEnabled": enabled, "storedFormat": 2}), encoding="utf-8")
    settings = load_app_settings()
    assert settings["billionContextEnabled"] is enabled
    assert settings["extractionBillionContextEnabled"] is enabled


def test_values_equal_to_old_release_defaults_read_as_unset(tmp_path, monkeypatch):
    """Files from 8.5.3 and earlier held every setting. A value equal to that release's default
    cannot be told apart from an untouched one, so the current default applies; other values stay."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(path))
    legacy = {"printTranslationLogInTerminal": True, "maxConcurrentJobs": 8, "billionContextEnabled": True,
              "extractionBillionContextEnabled": True, "mnemonEnabled": False, "extractionMnemonEnabled": True}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    settings = load_app_settings()
    assert settings["billionContextEnabled"] is False
    assert settings["extractionBillionContextEnabled"] is False
    assert settings["maxConcurrentJobs"] == 8
    assert settings["mnemonEnabled"] is False
    # After the first save the file carries the format marker, so a later explicit choice is kept.
    save_app_settings({**settings, "billionContextEnabled": True})
    assert load_app_settings()["billionContextEnabled"] is True


def test_only_changed_settings_are_written_so_defaults_can_change_later(tmp_path, monkeypatch):
    """Regression: every save wrote all defaults to disk, so a later change to a default
    (billion-context on -> off) never reached users who had saved any setting."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(path))
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(tmp_path))
    save_app_settings(load_app_settings())
    assert json.loads(path.read_text(encoding="utf-8")) == {"storedFormat": 2}
    save_app_settings({**load_app_settings(), "maxConcurrentJobs": 7})
    assert json.loads(path.read_text(encoding="utf-8")) == {"maxConcurrentJobs": 7, "storedFormat": 2}


def test_extraction_switch_stays_independent_when_omitting_defaults(tmp_path, monkeypatch):
    """The extraction switch falls back to the translation one when missing; it must not be
    dropped while that fallback would carry it, or it would start following the other switch."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("GALTRANSL_APP_SETTINGS_PATH", str(path))
    monkeypatch.setenv("GALTRANSL_RESOURCE_DIR", str(tmp_path))
    save_app_settings({**load_app_settings(), "billionContextEnabled": True, "extractionBillionContextEnabled": True})
    save_app_settings({**load_app_settings(), "billionContextEnabled": False})
    assert load_app_settings()["extractionBillionContextEnabled"] is True
    save_app_settings({**load_app_settings(), "billionContextEnabled": True, "extractionBillionContextEnabled": False})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "billionContextEnabled": True, "extractionBillionContextEnabled": False, "storedFormat": 2}
    assert load_app_settings()["extractionBillionContextEnabled"] is False


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
            "billionContextEnabled": True, "extractionBillionContextEnabled": False,
            "mnemonEnabled": True, "extractionMnemonEnabled": True, "requestRecordMode": "off"}
        assert update({"extractionBillionContextEnabled": True})["billionContextEnabled"] is True
        assert update({"maxConcurrentJobs": 3})["billionContextEnabled"] is True
        assert update({"billionContextEnabled": False})["maxConcurrentJobs"] == 3
        assert load_app_settings()["billionContextEnabled"] is False
        assert load_app_settings()["extractionBillionContextEnabled"] is True
        assert update({"extractionBillionContextEnabled": False})["billionContextEnabled"] is False
        assert update({"mnemonEnabled": False})["extractionMnemonEnabled"] is True
        assert update({"extractionMnemonEnabled": False}) == {
            "maxConcurrentJobs": 3, "printTranslationLogInTerminal": False,
            "billionContextEnabled": False, "extractionBillionContextEnabled": False,
            "mnemonEnabled": False, "extractionMnemonEnabled": False, "requestRecordMode": "off"}
        assert load_app_settings()["mnemonEnabled"] is False
        assert load_app_settings()["extractionMnemonEnabled"] is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
