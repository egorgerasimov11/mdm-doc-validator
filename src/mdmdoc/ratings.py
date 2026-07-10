#!/usr/bin/env python3
"""
ratings.py — the 👍/👎 ledger (D11e): one row per operator tap, latest tap per
run wins. Structure only (run_id / rating / ts) — no values, no model calls.
Down-votes feed the training queue ("label this next") and flag the run page.
"""
from __future__ import annotations

import json
import threading

from . import config, runstore

PATH_NAME = "ratings.jsonl"
_LOCK = threading.Lock()


def _path():
    return config.DATASET_DIR / PATH_NAME


def record(run_id: str, rating: str) -> dict:
    row = {"run_id": run_id, "rating": rating, "ts": runstore.now_iso()}
    with _LOCK:
        config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def latest() -> dict[str, str]:
    """run_id -> 'up' | 'down' (last tap wins)."""
    p = _path()
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            out[str(row.get("run_id"))] = str(row.get("rating"))
        except Exception:
            continue
    return out


def of(run_id: str) -> str:
    return latest().get(run_id, "")
