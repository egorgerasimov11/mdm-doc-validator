"""Thin Ollama client for the extraction layer.

Deliberately NOT model_client: that module prefers an ssh tunnel to the Mac
mini (which has no vision models) and silently re-encodes any image over
1.5 MB. Here the host is explicit and images are sent exactly as rendered.

Host: MDMDOC_OLLAMA_HOST > OLLAMA_HOST > http://localhost:11434. Never starts
a server, never opens a tunnel.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import requests


class OllamaError(RuntimeError):
    pass


def host() -> str:
    h = os.environ.get("MDMDOC_OLLAMA_HOST") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    if not h.startswith("http"):
        h = "http://" + h
    return h.rstrip("/")


def alive(h: str | None = None, timeout: float = 3.0) -> bool:
    try:
        r = requests.get(f"{h or host()}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def tags(h: str | None = None) -> list[dict]:
    r = requests.get(f"{h or host()}/api/tags", timeout=10)
    r.raise_for_status()
    return r.json().get("models", [])


def available_models(h: str | None = None) -> set[str]:
    try:
        return {m["name"] for m in tags(h)}
    except Exception:
        return set()


def show(model: str, h: str | None = None) -> dict:
    r = requests.post(f"{h or host()}/api/show", json={"name": model}, timeout=30)
    if r.status_code != 200:
        raise OllamaError(f"show {model}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def has_vision(model: str, h: str | None = None) -> bool:
    """/api/show is authoritative — /api/tags under-reports (gemma3:4b shows text-only there)."""
    try:
        info = show(model, h)
    except Exception:
        return False
    caps = info.get("capabilities") or []
    if "vision" in caps:
        return True
    mi = info.get("model_info") or {}
    return any(".vision." in k or "clip." in k for k in mi)


def context_length(model: str, h: str | None = None) -> int | None:
    try:
        mi = show(model, h).get("model_info") or {}
    except Exception:
        return None
    for k, v in mi.items():
        if k.endswith(".context_length"):
            return int(v)
    return None


def ps(h: str | None = None) -> list[dict]:
    try:
        r = requests.get(f"{h or host()}/api/ps", timeout=10)
        return r.json().get("models", []) if r.status_code == 200 else []
    except Exception:
        return []


def unload(model: str, h: str | None = None) -> None:
    """Evict a model from memory (keep_alive=0 on an empty generate)."""
    try:
        requests.post(f"{h or host()}/api/generate",
                      json={"model": model, "prompt": "", "keep_alive": 0}, timeout=60)
    except Exception:
        pass


def unload_all(h: str | None = None) -> list[str]:
    names = [m.get("name") or m.get("model") for m in ps(h)]
    for n in names:
        if n:
            unload(n, h)
    return [n for n in names if n]


def pull(model: str, h: str | None = None, progress=None, timeout: int = 7200) -> None:
    """Blocking pull with optional progress callback(status, completed, total)."""
    with requests.post(f"{h or host()}/api/pull", json={"name": model, "stream": True},
                       stream=True, timeout=timeout) as r:
        r.raise_for_status()
        import json
        for line in r.iter_lines():
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("error"):
                raise OllamaError(ev["error"])
            if progress:
                progress(ev.get("status", ""), ev.get("completed"), ev.get("total"))


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def generate(model: str, prompt: str, images: list[Path] | None = None, *,
             options: dict | None = None, keep_alive: str | int = "45m",
             timeout: int = 600, fmt: str | dict | None = None, system: str | None = None,
             think: bool | None = None, h: str | None = None) -> tuple[str, dict]:
    """One /api/generate call. Returns (text, stats). Raises OllamaError on HTTP/transport
    errors — the caller decides how to record it."""
    body: dict = {"model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive,
                  "options": dict(options or {})}
    if images:
        body["images"] = [_b64(p) for p in images]
    if fmt:
        body["format"] = fmt
    if system:
        body["system"] = system
    if think is not None:
        body["think"] = think
    t0 = time.time()
    try:
        r = requests.post(f"{h or host()}/api/generate", json=body, timeout=timeout)
    except requests.RequestException as e:
        raise OllamaError(f"{model}: {e.__class__.__name__}: {e}") from e
    if r.status_code != 200:
        raise OllamaError(f"{model}: HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    stats = {
        "latency_s": round(time.time() - t0, 2),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
        "eval_duration_s": round((data.get("eval_duration") or 0) / 1e9, 2),
        "load_duration_s": round((data.get("load_duration") or 0) / 1e9, 2),
        "done_reason": data.get("done_reason"),
    }
    return data.get("response", "") or "", stats


def warm(model: str, keep_alive: str = "45m", h: str | None = None, timeout: int = 600) -> dict:
    """Load the model (empty prompt) so the first real page is not charged the load time."""
    _, stats = generate(model, "", options={"num_predict": 1}, keep_alive=keep_alive,
                        timeout=timeout, h=h)
    return stats
