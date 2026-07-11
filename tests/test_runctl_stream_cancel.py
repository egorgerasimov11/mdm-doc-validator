"""H2: an in-flight analysis actually STOPS on Cancel. The pipeline's cancel
Event now reaches the model call — model_client streams from Ollama only when a
cancel Event is live, checks it per NDJSON chunk, and closes the connection so
Ollama aborts. These tests drive a FAKE streaming session (no network) to prove
the bail, the server-side close, and byte-parity of the non-streaming path."""
import json
import threading

import pytest

from mdmdoc import model_client as mc, runctl

json_dumps = json.dumps


class _FakeResp:
    def __init__(self, lines, on_chunk=None):
        self._lines = lines
        self._on_chunk = on_chunk
        self.closed = False

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self._lines[-1])

    def iter_lines(self):
        for ln in self._lines:
            if self._on_chunk:
                self._on_chunk()
            yield ln.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True


def _ndjson(chunks, done_extra=None):
    lines = [json.dumps({"response": c}) for c in chunks]
    lines.append(json.dumps({"response": "", "done": True, **(done_extra or {})}))
    return lines


@pytest.fixture()
def active_control():
    evt = threading.Event()
    tok = runctl.activate(runctl.RunControl(cancel=evt))
    yield evt
    runctl.deactivate(tok)


def test_streams_and_accumulates_when_control_active(monkeypatch, active_control):
    resp = _FakeResp(_ndjson(["Hel", "lo ", "world"]))
    seen = {}

    def fake_post(url, json=None, timeout=None, stream=None):
        seen["stream"] = stream
        seen["body_stream"] = json.get("stream") if json else None
        return resp

    monkeypatch.setattr(mc, "_SESSION", type("S", (), {"post": staticmethod(fake_post)}))
    monkeypatch.setattr(mc, "host", lambda: "http://x")
    out = mc.generate("TEXT", "hi")
    assert out == "Hello world"                 # accumulated == concatenated chunks
    assert seen["stream"] is True and seen["body_stream"] is True


def test_cancel_mid_stream_raises_and_closes(monkeypatch, active_control):
    calls = {"n": 0}

    def on_chunk():
        calls["n"] += 1
        if calls["n"] == 2:
            active_control.set()                # operator cancels after chunk 1

    resp = _FakeResp(_ndjson(["a", "b", "c", "d"]), on_chunk=on_chunk)
    monkeypatch.setattr(mc, "_SESSION",
                        type("S", (), {"post": staticmethod(lambda *a, **k: resp)}))
    monkeypatch.setattr(mc, "host", lambda: "http://x")
    with pytest.raises(runctl.CheckCanceled):
        mc.generate("TEXT", "hi")
    assert resp.closed is True                   # `with` closed it -> Ollama aborts


def test_cancel_is_not_retried(monkeypatch, active_control):
    # a CheckCanceled must propagate immediately, not be swallowed by the retry
    # loop (the bare `except Exception` used to eat it)
    active_control.set()
    resp = _FakeResp(_ndjson(["x"]))
    attempts = {"n": 0}

    def fake_post(*a, **k):
        attempts["n"] += 1
        return resp

    monkeypatch.setattr(mc, "_SESSION", type("S", (), {"post": staticmethod(fake_post)}))
    monkeypatch.setattr(mc, "host", lambda: "http://x")
    with pytest.raises(runctl.CheckCanceled):
        mc.generate("TEXT", "hi", retries=3)
    assert attempts["n"] == 1                     # one call, no retry storm


def test_no_control_takes_the_blocking_path(monkeypatch):
    # eval / CLI / tests: no cancel Event -> stream=False, byte-identical
    posted = {}

    def fake_post(url, json=None, timeout=None, stream=None):
        posted["stream_kw"] = stream
        posted["body_stream"] = json.get("stream")
        return _FakeResp([json_dumps({"response": "plain"})])

    monkeypatch.setattr(mc, "_SESSION", type("S", (), {"post": staticmethod(fake_post)}))
    monkeypatch.setattr(mc, "host", lambda: "http://x")
    assert runctl.cancellation_active() is False
    out = mc.generate("TEXT", "hi")
    assert out == "plain"
    assert posted["stream_kw"] is None and posted["body_stream"] is False


def test_bare_control_without_event_does_not_stream(monkeypatch):
    # eval activates RunControl(cancel=None): cancellation_active() is False
    tok = runctl.activate(runctl.RunControl(cancel=None))
    posted = {}

    def fake_post(url, json=None, timeout=None, stream=None):
        posted["stream"] = stream
        return _FakeResp([json_dumps({"response": "ok"})])

    try:
        monkeypatch.setattr(mc, "_SESSION", type("S", (), {"post": staticmethod(fake_post)}))
        monkeypatch.setattr(mc, "host", lambda: "http://x")
        assert runctl.cancellation_active() is False
        assert mc.generate("TEXT", "hi") == "ok"
        assert posted["stream"] is None
    finally:
        runctl.deactivate(tok)


def test_bail_if_canceled_is_noop_without_control():
    runctl.bail_if_canceled("x")                  # must not raise outside a run


def test_vision_streams_and_cancels(monkeypatch, active_control, tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n" + b"0" * 64)
    calls = {"n": 0}

    def on_chunk():
        calls["n"] += 1
        if calls["n"] == 2:
            active_control.set()

    resp = _FakeResp(_ndjson(["see", "n text"]), on_chunk=on_chunk)
    monkeypatch.setattr(mc, "_SESSION",
                        type("S", (), {"post": staticmethod(lambda *a, **k: resp)}))
    monkeypatch.setattr(mc, "host", lambda: "http://x")
    with pytest.raises(runctl.CheckCanceled):
        mc.vision("VISION", "read", [str(img)])
    assert resp.closed is True
