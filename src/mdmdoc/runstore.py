#!/usr/bin/env python3
"""
runstore.py — per-document run artifacts under runs/<sha16>/.

Every byte persisted by the pipeline flows through write() which enforces the
leak gate (privacy.assert_no_leak). Render images (full sensitive pixels) are
deleted after a successful run unless --keep-renders.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .privacy import assert_no_leak

_LAST = config.RUNS_DIR / ".last"


def run_dir(run_id: str, create: bool = True) -> Path:
    d = config.RUNS_DIR / run_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def render_dir(run_id: str) -> Path:
    d = run_dir(run_id) / "render"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(run_id: str, name: str, payload, secrets: list[str] | None = None) -> Path:
    blob = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    assert_no_leak(blob, secrets or [])
    p = run_dir(run_id) / name
    p.write_text(blob, encoding="utf-8")
    return p


def load(run_id: str, name: str):
    p = run_dir(run_id, create=False) / name
    if not p.exists():
        return None
    txt = p.read_text(encoding="utf-8")
    if name.endswith(".json"):
        try:
            return json.loads(txt)
        except Exception:
            return None
    return txt


def cleanup_renders(run_id: str) -> None:
    d = run_dir(run_id, create=False) / "render"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def mark_last(run_id: str) -> None:
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _LAST.write_text(run_id)


def resolve_run(spec: str) -> str | None:
    """'last' | run id | prefix | source file path -> run id."""
    if spec == "last":
        return _LAST.read_text().strip() if _LAST.exists() else None
    p = Path(spec).expanduser()
    if p.exists() and p.is_file():
        import hashlib
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        rid = h.hexdigest()[:16]
        return rid if (config.RUNS_DIR / rid).is_dir() else None
    if "/" in spec:
        return None
    if (config.RUNS_DIR / spec).is_dir():
        return spec
    matches = [d.name for d in config.RUNS_DIR.glob(f"{spec}*") if d.is_dir()]
    if len(matches) == 1:
        return matches[0]
    return None


def list_runs() -> list[dict]:
    out = []
    if not config.RUNS_DIR.exists():
        return out
    for d in sorted(config.RUNS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = load(d.name, "meta.json") or {}
        rep = load(d.name, "report.json") or {}
        out.append({"run_id": d.name, "file": Path(meta.get("path", "?")).name,
                    "doc_class": meta.get("doc_class", "?"),
                    "doc_type": rep.get("doc_type", "?"), "verdict": rep.get("verdict", "?"),
                    "ts": meta.get("ts", "")})
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
