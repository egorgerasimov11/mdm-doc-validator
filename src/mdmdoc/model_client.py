#!/usr/bin/env python3
"""
model_client.py — thin Ollama client (vendored from form-validator, trimmed).

Inference runs on the Mac mini ("victor") by default: its Ollama binds
127.0.0.1 only, so we reach it through an SSH tunnel (local :11435 ->
mini :11434) that is opened automatically when missing. Host resolution order:
  1. MDMDOC_OLLAMA_HOST / OLLAMA_HOST env (explicit override, no fallback)
  2. an already-open tunnel at http://127.0.0.1:11435
  3. auto-open the tunnel: ssh -f -N -L 11435:127.0.0.1:11434 mac-mini
  4. the local MacBook Ollama at :11434 IF it is already running (it is gated —
     this tool NEVER starts an Ollama server anywhere, it only tunnels to one).

Roles (override via env MDMDOC_<ROLE>, falls back to the form-validator MDM_VAL_* vars):
  VISION  qwen2.5vl:7b     transcribe scanned PDFs / images (Stage A)
  TEXT    qwen3:4b         text -> structured JSON extraction (Stage B; the trainable model)
  EMBED   nomic-embed-text few-shot diversity selection (optional)

All calls use keep_alive=0 so models are evicted after each stage — the mini
runs with OLLAMA_MAX_LOADED_MODELS=1, sequential single-model use fits it.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import warnings

warnings.filterwarnings("ignore")  # silence urllib3 LibreSSL notice

import requests

TUNNEL_PORT = int(os.environ.get("MDMDOC_TUNNEL_PORT", "11435"))
SSH_ALIAS = os.environ.get("MDMDOC_SSH_HOST", "mac-mini")
_TUNNEL_URL = f"http://127.0.0.1:{TUNNEL_PORT}"
_LOCAL_URL = "http://localhost:11434"

# trust_env=False: never do the macOS SystemConfiguration proxy lookup (hangs in
# daemon worker threads). All traffic is localhost/tunnel anyway.
_SESSION = requests.Session()
_SESSION.trust_env = False

_HOST: str | None = None
_HOST_SOURCE = ""


def _probe(url: str, timeout: float = 2.5) -> bool:
    try:
        r = _SESSION.get(f"{url}/api/tags", timeout=timeout)
        return r.ok
    except Exception:
        return False


def _open_tunnel() -> bool:
    """Open the SSH tunnel to the mini's Ollama. This tunnels to an ALREADY
    RUNNING server — it never starts one."""
    try:
        r = subprocess.run(
            ["ssh", "-f", "-N", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-o", "ExitOnForwardFailure=yes",
             "-L", f"{TUNNEL_PORT}:127.0.0.1:11434", SSH_ALIAS],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def host() -> str:
    """Resolve the Ollama endpoint (cached per process)."""
    global _HOST, _HOST_SOURCE
    if _HOST:
        return _HOST
    env = os.environ.get("MDMDOC_OLLAMA_HOST") or os.environ.get("OLLAMA_HOST")
    if env:
        _HOST, _HOST_SOURCE = env.rstrip("/"), "env override"
        return _HOST
    if _probe(_TUNNEL_URL):
        _HOST, _HOST_SOURCE = _TUNNEL_URL, f"existing SSH tunnel -> {SSH_ALIAS}"
        return _HOST
    if _open_tunnel() and _probe(_TUNNEL_URL, timeout=4):
        _HOST, _HOST_SOURCE = _TUNNEL_URL, f"new SSH tunnel -> {SSH_ALIAS}"
        return _HOST
    if _probe(_LOCAL_URL):
        _HOST, _HOST_SOURCE = _LOCAL_URL, "local Ollama (fallback — mini unreachable)"
        return _HOST
    raise OllamaUnavailable(
        f"No Ollama reachable: could not tunnel to the Mac mini (`ssh {SSH_ALIAS}`) and "
        f"nothing answers at {_LOCAL_URL}. Check that the mini is up (`ssh {SSH_ALIAS}`) — "
        "or start the local Ollama manually. This tool never starts an Ollama server itself."
    )


def host_source() -> str:
    return _HOST_SOURCE


def reset_host() -> None:
    """Forget the cached endpoint and model list so the next call re-probes.
    A long-lived server needs this when the SSH tunnel dies under it."""
    global _HOST, _HOST_SOURCE, _AVAILABLE
    _HOST, _HOST_SOURCE, _AVAILABLE = None, "", None


def _role_env(name: str, default: str) -> str:
    return os.environ.get(f"MDMDOC_{name}", os.environ.get(f"MDM_VAL_{name}", default))


ROLES = {
    "VISION": _role_env("VISION", "qwen2.5vl:7b"),
    # OUR custom model (Modelfile: system prompt + operator's few-shot exemplars
    # baked in, rebuilt on every retrain); falls back to stock qwen3:4b until it
    # has been created on the model host (mdmdoc train --modelfile --apply)
    "TEXT": _role_env("TEXT", "mdmdoc-extract"),
    # STRONG tier: escalation target when the fast tier leaves critical gaps
    "TEXT_STRONG": _role_env("TEXT_STRONG", "qwen3:14b"),
    "EMBED": _role_env("EMBED", "nomic-embed-text"),
}

_FALLBACKS = {
    "VISION": ["qwen2.5vl:7b", "qwen2.5vl:3b", "moondream:latest"],
    "TEXT": ["qwen3:4b", "qwen3:8b", "qwen3:1.7b", "llama3.2:3b"],
    "TEXT_STRONG": ["qwen3:14b", "qwen3:8b", "mdmdoc-extract", "qwen3:4b"],
    "EMBED": ["nomic-embed-text"],
}


def strong_distinct() -> bool:
    """False when TEXT_STRONG resolves to the same model as TEXT — escalation
    would be a pointless re-run."""
    return resolve("TEXT_STRONG") != resolve("TEXT")


class OllamaUnavailable(RuntimeError):
    pass


def preflight(timeout: int = 5) -> set[str]:
    """Resolve the endpoint and verify it answers. NEVER starts a server."""
    url = host()  # raises OllamaUnavailable with instructions when nothing reachable
    try:
        r = _SESSION.get(f"{url}/api/tags", timeout=timeout)
        r.raise_for_status()
        return {m["name"] for m in r.json().get("models", [])}
    except Exception as e:
        raise OllamaUnavailable(
            f"Ollama endpoint {url} ({_HOST_SOURCE}) stopped answering "
            f"({e.__class__.__name__})."
        ) from e


_AVAILABLE: set[str] | None = None


def available_models() -> set[str]:
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            r = _SESSION.get(f"{host()}/api/tags", timeout=10)
            _AVAILABLE = {m["name"] for m in r.json().get("models", [])}
        except Exception:
            _AVAILABLE = set()
    return _AVAILABLE


def resolve(role: str) -> str:
    """Return the model for a role, falling back if the preferred one is absent."""
    want = ROLES.get(role, role)
    avail = available_models()
    if not avail or want in avail or f"{want}:latest" in avail:
        return want
    for fb in _FALLBACKS.get(role, []):
        if fb in avail or f"{fb}:latest" in avail:
            return fb
    return want


def _extract_json(text: str):
    """Best-effort: pull the first JSON object/array out of a model reply."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    t = re.sub(r"(?is)<think>.*?</think>", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def _complete(url: str, body: dict, timeout: int, stream: bool) -> str:
    """Return the model's final text. stream=False is byte-identical to the
    former blocking call (eval / CLI / tests take this path). stream=True sends
    stream:true, iterates the NDJSON, checks runctl cancellation on EVERY chunk,
    and closes the response on cancel so Ollama ABORTS the in-flight generation
    server-side (real GPU/CPU savings, not just an early return)."""
    from . import runctl
    if not stream:
        r = _SESSION.post(url, json=body, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        return d.get("response", "") or d.get("thinking", "")
    resp_acc, think_acc = [], []
    # a streamed `timeout` is per-read (inactivity), not a total cap — keep the
    # old total-wallclock ceiling with an explicit deadline
    deadline = time.monotonic() + timeout if isinstance(timeout, (int, float)) else 0
    with _SESSION.post(url, json={**body, "stream": True}, timeout=timeout, stream=True) as r:
        r.raise_for_status()                       # a 400 still surfaces HERE, body readable
        for line in r.iter_lines():
            runctl.bail_if_canceled("model")       # raises -> `with` closes r -> Ollama stops
            if deadline and time.monotonic() > deadline:
                raise requests.Timeout(f"stream exceeded {timeout}s")
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("response"):
                resp_acc.append(obj["response"])
            if obj.get("thinking"):
                think_acc.append(obj["thinking"])
            if obj.get("done"):
                break
    return "".join(resp_acc) or "".join(think_acc)


def generate(role_or_model: str, prompt: str, system: str | None = None,
             options: dict | None = None, fmt: str | None = None,
             keep_alive=0, timeout: int = 300, retries: int = 1, think: bool = False) -> str:
    """think=False disables the qwen3 'thinking' channel so tokens go to the answer."""
    from . import runctl
    model = resolve(role_or_model) if role_or_model in ROLES else role_or_model
    body = {"model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive,
            "think": think,
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 1536, **(options or {})}}
    if system:
        body["system"] = system
    if fmt:
        body["format"] = fmt
    stream = runctl.cancellation_active()          # only a cancelable server job streams
    last = ""
    for attempt in range(retries + 1):
        try:
            return _complete(f"{host()}/api/generate", body, timeout, stream)
        except runctl.CheckCanceled:
            raise                                  # a cancel must NEVER be retried
        except Exception as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
    return f"[model_client error: {last}]"


def generate_json(role_or_model: str, prompt: str, system: str | None = None,
                  options: dict | None = None, timeout: int = 300, retries: int = 2,
                  think: bool = False):
    """Generate with format=json and parse. Returns (obj_or_None, first_try_ok)."""
    first_try_ok = True
    for attempt in range(retries + 1):
        txt = generate(role_or_model, prompt, system=system, options=options,
                       fmt="json", timeout=timeout, think=think)
        obj = _extract_json(txt)
        if obj is not None:
            return obj, first_try_ok
        first_try_ok = False
        prompt += "\n\nReturn ONLY valid JSON, nothing else."
    return None, False


# last vision failure detail (reset on success) — callers that swallow a None
# JSON result can still surface WHY the read failed (runs are serialized under
# PIPELINE_LOCK, so a module-level slot is safe enough for the operator console)
LAST_VISION_ERROR = ""
# last payload-guard action (trimmed image count / downscale) — informational
LAST_VISION_NOTE = ""

# Payload guards (P1, quality wave): image tokens count against num_ctx, and
# several full-page renders in ONE /api/generate overflow it (real case: an
# 8-page W-8 scan -> 3 pages @170dpi in one transcribe -> HTTP 400). Caps are
# env-tunable; the degrade path drops to 1 image on a context-shaped 400.
VISION_MAX_IMAGES_PER_CALL = int(os.environ.get("MDMDOC_VISION_MAX_IMAGES", "2"))
VISION_MAX_IMAGE_BYTES = int(os.environ.get("MDMDOC_VISION_MAX_IMG_BYTES", "1500000"))


def _load_image_capped(path: str) -> bytes | None:
    """Read an image file; if it exceeds VISION_MAX_IMAGE_BYTES, downscale
    in memory (JPEG q85, shrinking iterations). NEVER returns more bytes
    than the original — for JPEG-hostile content the original wins."""
    try:
        data = open(path, "rb").read()
    except Exception:
        return None
    if len(data) <= VISION_MAX_IMAGE_BYTES:
        return data
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        best = data
        factor = (VISION_MAX_IMAGE_BYTES / len(data)) ** 0.5
        for _ in range(3):
            w = max(640, int(img.width * factor))
            h = max(640, int(img.height * factor))
            out = io.BytesIO()
            img.resize((w, h)).save(out, format="JPEG", quality=85)
            enc = out.getvalue()
            if len(enc) < len(best):
                best = enc
            if len(enc) <= VISION_MAX_IMAGE_BYTES or (w, h) == (640, 640):
                break
            factor *= 0.7
        return best
    except Exception:
        return data   # better an oversized attempt than none


def vision(role_or_model: str, prompt: str, images: list[str], system: str | None = None,
           options: dict | None = None, keep_alive=0, timeout: int = 420, retries: int = 2,
           fmt: str | None = None) -> str:
    global LAST_VISION_ERROR, LAST_VISION_NOTE
    LAST_VISION_NOTE = ""
    model = resolve(role_or_model) if role_or_model in ROLES else role_or_model
    b64 = []
    downscaled = 0
    for p in images:
        raw_bytes = _load_image_capped(p)
        if raw_bytes is None:
            continue
        try:
            if len(raw_bytes) < os.path.getsize(p):
                downscaled += 1
        except OSError:
            pass
        b64.append(base64.b64encode(raw_bytes).decode("ascii"))
    if not b64:
        return "[vision: no readable images]"
    notes = []
    if len(b64) > VISION_MAX_IMAGES_PER_CALL:
        notes.append(f"vision payload trimmed to {VISION_MAX_IMAGES_PER_CALL} "
                     f"of {len(b64)} images")
        b64 = b64[:VISION_MAX_IMAGES_PER_CALL]
    if downscaled:
        notes.append(f"{downscaled} image(s) downscaled to fit the payload cap")
    LAST_VISION_NOTE = "; ".join(notes)

    def _body(imgs: list[str]) -> dict:
        body = {"model": model, "prompt": prompt, "images": imgs, "stream": False,
                "keep_alive": keep_alive,
                # num_ctx: image tokens count against the context window; a large
                # screenshot alone can exceed Ollama's 4096 default (real case: a 2x
                # SAP capture = ~4k image tokens -> HTTP 400 exceed_context_size)
                "options": {"temperature": 0.1, "num_ctx": 16384, "num_predict": 2048,
                            **(options or {})}}
        if system:
            body["system"] = system
        if fmt:
            body["format"] = fmt
        return body

    from . import runctl
    stream = runctl.cancellation_active()          # only a cancelable server job streams
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = _complete(f"{host()}/api/generate", _body(b64), timeout, stream)
            if resp.strip():
                LAST_VISION_ERROR = ""
                return resp
            last = "empty response"
        except runctl.CheckCanceled:
            raise                                  # a cancel must NEVER be retried
        except Exception as e:
            last = str(e)
            # the Ollama error BODY names the real cause (e.g. "request (4124
            # tokens) exceeds the available context size") — don't hide it
            body_txt = getattr(getattr(e, "response", None), "text", "") or ""
            if body_txt:
                last += f" — {body_txt[:200]}"
            # context-shaped failure with a multi-image payload: degrade once
            # to a single image before the next attempt (P1)
            low = last.lower()
            if len(b64) > 1 and ("context" in low or "400" in low):
                b64 = b64[:1]
                LAST_VISION_NOTE = (LAST_VISION_NOTE + "; " if LAST_VISION_NOTE else "") \
                    + "degraded to 1 image after context-overflow error"
        time.sleep(2 * (attempt + 1))
    LAST_VISION_ERROR = (f"{last} — {len(b64)} image(s), "
                         f"{sum(map(len, b64)) // 1024} KB b64")
    return f"[vision error: {LAST_VISION_ERROR}]"


def generate_json_vision(prompt: str, images: list[str], retries: int = 2):
    """Vision call constrained to JSON output. Returns (obj_or_None, first_try_ok)."""
    first = True
    for attempt in range(retries + 1):
        txt = vision("VISION", prompt, images, fmt="json",
                     options={"temperature": 0, "seed": 7})
        obj = _extract_json(txt)
        if obj is not None:
            return obj, first
        first = False
        prompt += "\n\nReturn ONLY valid JSON, nothing else."
    return None, False


def embed(texts: list[str], timeout: int = 120) -> list[list[float]]:
    model = resolve("EMBED")
    vecs = []
    for t in texts:
        try:
            r = _SESSION.post(f"{host()}/api/embeddings",
                              json={"model": model, "prompt": t[:8000]}, timeout=timeout)
            r.raise_for_status()
            vecs.append(r.json().get("embedding", []))
        except Exception:
            vecs.append([])
    return vecs


def unload(role_or_model: str) -> None:
    """Ask Ollama to evict a model now (free RAM between stages)."""
    model = resolve(role_or_model) if role_or_model in ROLES else role_or_model
    try:
        _SESSION.post(f"{host()}/api/generate",
                      json={"model": model, "keep_alive": 0, "prompt": ""}, timeout=20)
    except Exception:
        pass
