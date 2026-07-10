"""D4: the effort slider — profile table, thread-isolated overrides, the
byte-frozen fast tier, API validation, settings persistence."""
import threading

import pytest

from mdmdoc import config, runctl
from mdmdoc.estimate import shape_key


def test_profile_table():
    p1 = config.effort_profile(1)
    assert p1["engine"] == "deterministic" and p1["overrides"]["ladder_enabled"] is False
    p2 = config.effort_profile(2)
    assert p2["overrides"]["strong_enabled"] is False
    p3 = config.effort_profile(3)
    assert p3 == {"engine": "auto", "quality": False, "overrides": {}}
    p4 = config.effort_profile(4)
    assert p4["quality"] is True and p4["overrides"]["ladder_pages"] == 3
    p5 = config.effort_profile(5)
    assert p5["engine"] == "llm-first"
    assert p5["overrides"]["strong_model_options"]["num_predict"] == 4096
    assert p5["overrides"]["strong_think"] is True
    assert config.effort_profile(99)["engine"] == "llm-first"   # clamped


def test_effort_overrides_are_thread_isolated():
    token = runctl.activate(runctl.RunControl(
        overrides=config.effort_profile(5)["overrides"]))
    other = {}

    def sibling():
        other["cap"] = config.sig_vision_cap()

    try:
        assert config.sig_vision_cap() == 6
        assert config.time_budget_s() == 420
        t = threading.Thread(target=sibling)
        t.start()
        t.join()
    finally:
        runctl.deactivate(token)
    assert other["cap"] == 4                       # sibling thread saw defaults


def test_fast_tier_call_is_byte_frozen(monkeypatch):
    """Effort 5 may widen ONLY the strong tier: the fast TEXT call's options
    and think flag must be identical at every effort level."""
    from mdmdoc import stage_b
    from mdmdoc.stage_a import RawDoc
    calls = []

    def fake_generate_json(role, prompt, system=None, options=None, timeout=300,
                           retries=2, think=False):
        calls.append({"role": role, "options": dict(options or {}), "think": think})
        return {"doc_type": "bank_letter", "fields": {}}, True

    monkeypatch.setattr(stage_b.mc, "generate_json", fake_generate_json)
    monkeypatch.setattr(stage_b.mc, "unload", lambda role: None)
    monkeypatch.setattr(stage_b.mc, "resolve", lambda role: "qwen3:4b")
    raw = RawDoc(path="x.pdf", sha256="a" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = "Bank confirmation letter"

    stage_b._run_model(raw, "TEXT")                       # no effort
    token = runctl.activate(runctl.RunControl(
        overrides=config.effort_profile(5)["overrides"]))
    try:
        stage_b._run_model(raw, "TEXT")                   # effort 5
        stage_b._run_model(raw, "TEXT_STRONG")            # strong widened
    finally:
        runctl.deactivate(token)

    assert calls[0] == calls[1]                           # fast tier frozen
    assert calls[0]["options"] == {"num_ctx": 16384, "temperature": 0, "seed": 7}
    assert calls[0]["think"] is False
    assert calls[2]["options"]["num_predict"] == 4096     # strong widened
    assert calls[2]["think"] is True


def test_effort2_disables_strong_tier():
    token = runctl.activate(runctl.RunControl(
        overrides=config.effort_profile(2)["overrides"]))
    try:
        assert runctl.override("strong_enabled", True) is False
    finally:
        runctl.deactivate(token)
    assert runctl.override("strong_enabled", True) is True


def test_shape_key_stable_for_default_effort():
    base = shape_key("bank", True, True, False, False)
    assert shape_key("bank", True, True, False, False, effort=3) == base
    assert shape_key("bank", True, True, False, False, effort=None) == base
    assert shape_key("bank", True, True, False, False, effort=5).endswith("|e5")


def test_api_validates_effort_and_persists_default(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mdmdoc.server.app import create_app
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    client = TestClient(create_app("full"))
    r = client.post("/api/v1/check", data={"doc_class": "bank", "effort": "9"},
                    files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 400
    r = client.post("/api/v1/settings", json={"default_effort": 4})
    assert r.status_code == 200 and r.json()["default_effort"] == 4
    r = client.get("/api/v1/settings")
    assert r.json()["default_effort"] == 4
    r = client.post("/api/v1/settings", json={"default_effort": 0})
    assert r.status_code == 400
