#!/usr/bin/env python3
"""
challenges.py — the rule-challenge ledger (E4/E5): every time the operator's
judgement contradicts a rule (Mark valid over a stricter finding, or a 👎 on a
specific finding), one row lands here. Challenged rules surface in the
Approvals panel and in rule_stats as demotion evidence until the operator
edits, rejects, deletes — or dismisses the challenge.

Rows: {ts, rule_id, doc_class, run_id, kind: valid-mark|finding-downvote|
dismissed, note?}. Live count = all challenges minus dismissals. Structure
only — no document values, no model calls. Same append idiom as ratings.py.
"""
from __future__ import annotations

import json
import threading

from . import config, runstore

PATH_NAME = "challenges.jsonl"
_LOCK = threading.Lock()


def _path():
    return config.RULES_DIR / PATH_NAME


def record(rule_id: str, doc_class: str, run_id: str, kind: str,
           note: str = "") -> dict:
    row = {"ts": runstore.now_iso(), "rule_id": str(rule_id)[:40],
           "doc_class": str(doc_class)[:8], "run_id": str(run_id)[:32],
           "kind": kind}
    if note:
        row["note"] = str(note)[:200]
    with _LOCK:
        config.RULES_DIR.mkdir(parents=True, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def dismiss_rule(rule_id: str, reason: str = "") -> dict:
    """One dismissal row neutralizes ALL prior challenges of the rule (the
    operator looked and decided the rule stands / the rule is gone)."""
    return record(rule_id, "", "", "dismissed", note=reason)


def retract(rule_id: str, run_id: str, note: str = "") -> dict:
    """Undo of ONE challenge (F1): surgically takes back a single vote without
    the nuclear dismissal — counts() subtracts it, for_rule() drops the newest
    row of the same run."""
    return record(rule_id, "", run_id, "retracted", note=note)


def load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def counts() -> dict[str, int]:
    """rule_id -> LIVE challenge count (challenges after the last dismissal,
    minus retractions)."""
    live: dict[str, int] = {}
    for r in load():
        rid = str(r.get("rule_id") or "")
        if not rid:
            continue
        kind = r.get("kind")
        if kind == "dismissed":
            live[rid] = 0
        elif kind == "retracted":
            live[rid] = max(0, live.get(rid, 0) - 1)
        else:
            live[rid] = live.get(rid, 0) + 1
    return {k: v for k, v in live.items() if v > 0}


def for_rule(rule_id: str, limit: int = 10) -> list[dict]:
    """Latest live challenge rows for one rule (newest first), for the panel."""
    rows = [r for r in load() if r.get("rule_id") == rule_id]
    out: list[dict] = []
    for r in rows:                      # respect the last dismissal boundary
        kind = r.get("kind")
        if kind == "dismissed":
            out = []
        elif kind == "retracted":       # take back the newest row of that run
            run = r.get("run_id") or ""
            for i in range(len(out) - 1, -1, -1):
                if not run or out[i].get("run_id") == run:
                    del out[i]
                    break
        else:
            out.append(r)
    return list(reversed(out))[:limit]
