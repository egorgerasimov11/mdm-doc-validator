#!/usr/bin/env python3
"""
oplog.py — the operator action audit ledger (E1): every mutating operator
action and every background job's start/end lands as one append-only JSONL
row, so the console has a HISTORY that survives navigation, restarts and
memory caps.

Structure only, PII-free by construction: ids, action names, rule ids,
job kinds and short details — never document values. Same append idiom as
ratings.py / patterns.py (_LOCK + one JSON line).
"""
from __future__ import annotations

import json
import threading
import uuid

from . import config, runstore

PATH_NAME = "oplog.jsonl"
_LOCK = threading.Lock()

# canonical action names (free-form allowed, these are the known set the
# history page offers as filter chips)
ACTIONS = ("check", "cancel", "label", "mark-valid", "teach-type", "rating",
           "finding-vote",
           "rule-save", "rule-approve", "rule-tier", "rule-delete",
           "rule-restore", "rule-create", "rule-challenge-dismiss",
           "rule-regenerate", "skill-upload", "settings", "train", "eval",
           "bulk", "run-test", "job-start", "job-end", "pattern-study", "undo")


def _path():
    return config.DATASET_DIR / PATH_NAME


def log(action: str, run_id: str = "", rule_id: str = "", doc_class: str = "",
        job_id: str = "", detail: str = "") -> dict:
    # `op` uniquely names this row so undo can target it — ts alone collides
    # (second precision, bulk-approve logs several rows per second)
    row = {"ts": runstore.now_iso(), "op": uuid.uuid4().hex[:8],
           "action": str(action)[:40]}
    if run_id:
        row["run_id"] = str(run_id)[:32]
    if rule_id:
        row["rule_id"] = str(rule_id)[:40]
    if doc_class:
        row["doc_class"] = str(doc_class)[:8]
    if job_id:
        row["job_id"] = str(job_id)[:16]
    if detail:
        row["detail"] = str(detail)[:200]
    try:
        with _LOCK:
            config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass   # the audit trail must never break an operator action
    return row


def recent(limit: int = 500, actions: tuple | None = None) -> list[dict]:
    """Newest first. `actions` filters by action name (prefix match so
    'job-' covers start+end)."""
    p = _path()
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if actions:
        rows = [r for r in rows
                if any(str(r.get("action", "")).startswith(a) for a in actions)]
    return list(reversed(rows[-limit:] if limit else rows))
