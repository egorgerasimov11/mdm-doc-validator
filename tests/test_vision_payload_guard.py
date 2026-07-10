"""P1 (quality wave): the vision payload guard. Several full-page renders in
one /api/generate overflowed the model context (recorded HTTP 400 on a real
8-page W-8 scan). Caps: image count per call, per-image bytes (in-memory
downscale), one-shot degrade to a single image on a context-shaped error."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mdmdoc import model_client as mc


class _Recorder:
    def __init__(self, responses=None, errors=None):
        self.bodies = []
        self._responses = list(responses or [])
        self._errors = list(errors or [])

    def post(self, url, json=None, timeout=None):
        self.bodies.append(json)
        if self._errors:
            err = self._errors.pop(0)
            if err is not None:
                raise err
        text = self._responses.pop(0) if self._responses else "ok"
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"response": text})


def _http_400(msg: str):
    import requests
    resp = SimpleNamespace(text=msg, status_code=400)
    err = requests.HTTPError(f"400 Client Error: Bad Request — {msg}")
    err.response = resp
    return err


def _png(tmp_path: Path, name: str, side: int = 40) -> str:
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (side, side), "white").save(p, format="PNG")
    return str(p)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mc.time, "sleep", lambda *a: None)


def test_vision_trims_image_count(tmp_path, monkeypatch):
    rec = _Recorder(responses=["transcript"])
    monkeypatch.setattr(mc, "_SESSION", rec)
    monkeypatch.setattr(mc, "host", lambda: "http://fake")
    imgs = [_png(tmp_path, f"p{i}.png") for i in range(4)]
    out = mc.vision("VISION", "read", imgs)
    assert out == "transcript"
    assert len(rec.bodies[0]["images"]) == mc.VISION_MAX_IMAGES_PER_CALL == 2
    assert "trimmed to 2 of 4" in mc.LAST_VISION_NOTE


def test_vision_downscale_by_bytes(tmp_path, monkeypatch):
    """Oversized image is re-encoded smaller before base64."""
    import base64

    from PIL import Image
    rec = _Recorder(responses=["ok"])
    monkeypatch.setattr(mc, "_SESSION", rec)
    monkeypatch.setattr(mc, "host", lambda: "http://fake")
    monkeypatch.setattr(mc, "VISION_MAX_IMAGE_BYTES", 50_000)
    # large noisy JPEG (scan-like) -> over the cap, shrinks when downscaled
    img = Image.new("RGB", (2400, 2400))
    img.putdata([(x * 7 % 255, x * 13 % 255, x * 29 % 255)
                 for x in range(2400 * 2400)])
    p = tmp_path / "big.jpg"
    img.save(p, format="JPEG", quality=95)
    assert p.stat().st_size > 50_000
    mc.vision("VISION", "read", [str(p)])
    sent = base64.b64decode(rec.bodies[0]["images"][0])
    assert len(sent) < p.stat().st_size
    assert "downscaled" in mc.LAST_VISION_NOTE


def test_vision_400_context_degrades_to_one_image(tmp_path, monkeypatch):
    rec = _Recorder(responses=["recovered"],
                    errors=[_http_400("request (41240 tokens) exceeds the available "
                                      "context size"), None])
    monkeypatch.setattr(mc, "_SESSION", rec)
    monkeypatch.setattr(mc, "host", lambda: "http://fake")
    imgs = [_png(tmp_path, f"q{i}.png") for i in range(2)]
    out = mc.vision("VISION", "read", imgs)
    assert out == "recovered"
    assert len(rec.bodies[0]["images"]) == 2
    assert len(rec.bodies[1]["images"]) == 1          # degraded on retry
    assert "degraded to 1 image" in mc.LAST_VISION_NOTE


def test_vision_error_carries_payload_stats(tmp_path, monkeypatch):
    rec = _Recorder(errors=[_http_400("boom"), _http_400("boom"), _http_400("boom")])
    monkeypatch.setattr(mc, "_SESSION", rec)
    monkeypatch.setattr(mc, "host", lambda: "http://fake")
    out = mc.vision("VISION", "read", [_png(tmp_path, "x.png")], retries=2)
    assert out.startswith("[vision error:")
    assert "image(s)" in out and "KB b64" in out


def test_deep_read_chunks_transcribe_calls(tmp_path, monkeypatch):
    import fitz

    from mdmdoc import stage_a
    from mdmdoc.stage_a import RawDoc, _deep_read_pages
    p = tmp_path / "t.pdf"
    d = fitz.open()
    for i in range(3):
        pg = d.new_page()
        pg.insert_text((72, 100), f"page {i} bank account data words here")
    d.save(p)
    d.close()
    calls = []

    def fake_vision(role, prompt, images, **kw):
        calls.append(list(images))
        return f"chunk{len(calls)}"

    monkeypatch.setattr(stage_a.mc, "vision", fake_vision)
    monkeypatch.setattr(stage_a.mc, "VISION_MAX_IMAGES_PER_CALL", 2, raising=False)
    monkeypatch.setattr(stage_a.mc, "LAST_VISION_NOTE", "", raising=False)
    raw = RawDoc(path=str(p), sha256="a" * 16, ext=".pdf", doc_class="w9")
    picks = [(1, 0, "", 0), (1, 1, "", 0), (1, 2, "", 0)]
    _deep_read_pages(p, picks, tmp_path, raw, use_vision=True)
    assert len(calls) == 2                       # 2+1 images -> two calls
    assert [len(c) for c in calls] == [2, 1]
    assert raw.vision_text == "chunk1\nchunk2"
