#!/usr/bin/env python3
"""bulk.webcheck — OPT-IN live existence checks for the bank bulk case.

Reuses the document validator's web_enrichment 3-source routing ladder
(usbanklocations -> paymentlabs -> wise; egress = the ROUTING NUMBER only,
a public identifier). Results are cached in inbox/bulk_cache_routing.json so
a 7k-row export costs one lookup per UNIQUE routing, and re-runs are free.
Network trouble degrades gracefully: after 3 consecutive UNAVAILABLE answers
the remaining routings are marked unchecked — a bulk run never fails because
the network is down.
"""
from __future__ import annotations

import json
import time

from .. import config

CACHE_NAME = "bulk_cache_routing.json"
_MAX_CONSECUTIVE_DOWN = 3


def _cache_path():
    return config.INBOX_DIR / CACHE_NAME


def _load_cache() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    config.ensure_dirs()
    config.atomic_write_text(_cache_path(),
                             json.dumps(cache, ensure_ascii=False, indent=1))


def routing_existence(routings: list[str], progress=None) -> dict:
    """-> {routing: {'status': found|not_found|unavailable|unchecked,
                     'note': str, 'ts': iso}} for every UNIQUE input routing."""
    from ..privacy import SecretVault
    from ..runstore import now_iso
    from ..web_enrichment.aba import _directory_evidence

    say = progress or (lambda s: None)
    cache = _load_cache()
    out: dict = {}
    todo = []
    for r in dict.fromkeys(routings):          # unique, order-preserving
        hit = cache.get(r)
        if hit and hit.get("status") in ("found", "not_found"):
            out[r] = hit                        # misses/unavailable are retried
        else:
            todo.append(r)
    say(f"web: {len(out)} routings from cache, {len(todo)} to look up")

    down_streak = 0
    for i, aba in enumerate(todo, start=1):
        if down_streak >= _MAX_CONSECUTIVE_DOWN:
            out[aba] = {"status": "unchecked",
                        "note": "skipped — directories unreachable", "ts": now_iso()}
            continue
        ev = _directory_evidence(aba, SecretVault())
        entry = {"status": ev.status, "note": ev.label, "ts": now_iso(),
                 "url": ev.source_url}
        if ev.status == "unavailable":
            down_streak += 1
        else:
            down_streak = 0
            cache[aba] = entry                  # only definitive answers cached
        out[aba] = entry
        if i % 10 == 0 or i == len(todo):
            say(f"web: looked up {i}/{len(todo)} routings")
            _save_cache(cache)
        time.sleep(0.4)                         # be polite to public directories
    _save_cache(cache)
    return out
