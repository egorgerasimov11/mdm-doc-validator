#!/usr/bin/env python3
"""
tags.py — operator tags for the Activity feed. Same append-only, PII-free idiom
as ratings.py / oplog.py. Two stores under dataset/:

  tags.json       — the tag DEFINITIONS [{id, name, group, color}], rewritten
                    atomically (config.atomic_write_text) on create/delete.
  tag_links.jsonl — append-only assignment log {entity, tag, on, ts};
                    assignments() collapses it last-write-per-(entity,tag) wins.

An `entity` is "<kind>:<id>" (doc:/consol:/data:) so a document sha, a case id and
a bulk id never collide. Definitions and links carry structure only — no document
values ever pass through here.
"""
from __future__ import annotations

import json
import threading
import uuid

from . import config, runstore

_LOCK = threading.Lock()
DEFS_NAME = "tags.json"
LINKS_NAME = "tag_links.jsonl"
# distinct, theme-safe swatch colors handed out round-robin on create
PALETTE = ["#0969da", "#1f883d", "#b58a00", "#8250df", "#d1242f", "#0a7ea4", "#bf3989"]


def _defs_path():
    return config.DATASET_DIR / DEFS_NAME


def _links_path():
    return config.DATASET_DIR / LINKS_NAME


def list_defs() -> list[dict]:
    try:
        data = json.loads(_defs_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_defs(defs: list[dict]) -> None:
    config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
    config.atomic_write_text(
        _defs_path(), json.dumps(defs, ensure_ascii=False, indent=1) + "\n")


def create(name: str, group: str = "", color: str = "") -> dict:
    name = str(name).strip()[:40]
    if not name:
        raise ValueError("tag name required")
    with _LOCK:
        defs = list_defs()
        tag = {"id": uuid.uuid4().hex[:8], "name": name,
               "group": str(group).strip()[:24],
               "color": (color or PALETTE[len(defs) % len(PALETTE)])}
        defs.append(tag)
        _write_defs(defs)
    return tag


def _log_link(entity: str, tag_id: str, on: bool) -> None:
    row = {"entity": str(entity)[:48], "tag": str(tag_id)[:8],
           "on": bool(on), "ts": runstore.now_iso()}
    with _LOCK:
        config.DATASET_DIR.mkdir(parents=True, exist_ok=True)
        with open(_links_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def delete(tag_id: str) -> None:
    with _LOCK:
        _write_defs([t for t in list_defs() if t.get("id") != tag_id])
    # tombstone every live assignment so assignments() drops it too
    for ent, ids in _raw_assignments().items():
        if tag_id in ids:
            _log_link(ent, tag_id, False)


def assign(entity: str, tag_id: str, on: bool = True) -> None:
    if on and tag_id not in {t["id"] for t in list_defs()}:
        raise ValueError("unknown tag")
    _log_link(entity, tag_id, on)


def _raw_assignments() -> dict[str, set]:
    """entity -> set(tag_id) from the raw log, last write per (entity,tag) wins.
    Does not filter against live definitions (delete() needs the raw view)."""
    p = _links_path()
    out: dict[str, set] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ent, tid, on = str(r.get("entity")), str(r.get("tag")), bool(r.get("on"))
        s = out.setdefault(ent, set())
        s.add(tid) if on else s.discard(tid)
    return out


def assignments() -> dict[str, set]:
    """entity -> set(tag_id), tags that no longer exist filtered out."""
    live = {t["id"] for t in list_defs()}
    return {e: (ids & live) for e, ids in _raw_assignments().items() if (ids & live)}


def for_entity(entity: str) -> list[str]:
    return sorted(assignments().get(entity, set()))


def list_with_counts() -> list[dict]:
    a = assignments()
    counts: dict[str, int] = {}
    for ids in a.values():
        for tid in ids:
            counts[tid] = counts.get(tid, 0) + 1
    return [{**t, "count": counts.get(t["id"], 0)} for t in list_defs()]
