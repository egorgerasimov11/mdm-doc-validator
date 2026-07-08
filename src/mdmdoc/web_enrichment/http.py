#!/usr/bin/env python3
"""
http.py — the ONLY way web_enrichment reaches the network.

Properties:
  * opt-in       — callers only reach here when web evidence is enabled;
  * egress-gated — the fully-rendered URL (with query string) is passed through
                   egress.assert_safe_outbound BEFORE any socket is opened;
  * hardened     — trust_env=False (no macOS proxy lookup / no env proxies),
                   short timeout, a descriptive User-Agent (SEC EDGAR requires
                   an identifying UA), one retry-less attempt;
  * fail-soft    — ANY error (offline, DNS, timeout, non-200, bad JSON) returns
                   None. A degraded network never crashes a run and never
                   changes a verdict — the connector simply reports 'unavailable'.
"""
from __future__ import annotations

import os
import time

import requests

from . import egress

# A contactable, honest User-Agent. SEC EDGAR blocks generic UAs; other
# registries are indifferent. Overridable so an operator can add contact info.
USER_AGENT = os.environ.get(
    "MDMDOC_WEB_USER_AGENT",
    "mdmdoc/0.1 (MDM document validator; local evidence layer; +https://localhost)")

_SESSION = requests.Session()
_SESSION.trust_env = False   # never consult system/env proxies (localhost-safe posture)


def timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get("MDMDOC_WEB_TIMEOUT", "6")))
    except ValueError:
        return 6.0


def cache_ttl_s() -> float:
    """In-memory TTL cache for registry answers (default 24h; 0 disables).
    Registries change slowly; re-querying FDIC/GLEIF/SEC for the same bank on
    every run wastes their rate limits and our latency. Per-process only —
    nothing is persisted to disk."""
    try:
        return max(0.0, float(os.environ.get("MDMDOC_WEB_CACHE_TTL", "86400")))
    except ValueError:
        return 86400.0


_CACHE: dict[str, tuple[float, object]] = {}


def get_json(url: str, params: dict | None = None, vault=None,
             headers: dict | None = None) -> object | None:
    """GET url with params and return parsed JSON, or None on ANY failure.

    Before the request is issued, the rendered URL+query is checked by the
    egress gate; a forbidden value raises EgressBlocked (this propagates — a
    would-be leak is a bug to fix, not a network condition to swallow)."""
    req = requests.Request("GET", url, params=params or {},
                           headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                    **(headers or {})})
    prepared = _SESSION.prepare_request(req)
    egress.assert_safe_outbound(prepared.url or url, vault)   # may raise EgressBlocked
    key = prepared.url or url
    ttl = cache_ttl_s()
    if ttl:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    for attempt in (0, 1):   # one polite retry on 429/5xx — then degrade
        try:
            r = _SESSION.send(prepared, timeout=timeout_s(), allow_redirects=True)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                return None
            if not r.ok:
                return None
            data = r.json()
            if ttl:
                _CACHE[key] = (time.time(), data)
            return data
        except egress.EgressBlocked:
            raise
        except Exception:
            if attempt == 0:
                time.sleep(1.5)
                continue
            return None   # offline / DNS / timeout / TLS / non-JSON — degrade gracefully
    return None


def get_text(url: str, params: dict | None = None, vault=None,
             headers: dict | None = None) -> str | None:
    """Like get_json but returns the raw text body (for atom/xml sources)."""
    req = requests.Request("GET", url, params=params or {},
                           headers={"User-Agent": USER_AGENT, **(headers or {})})
    prepared = _SESSION.prepare_request(req)
    egress.assert_safe_outbound(prepared.url or url, vault)
    try:
        r = _SESSION.send(prepared, timeout=timeout_s(), allow_redirects=True)
        return r.text if r.ok else None
    except egress.EgressBlocked:
        raise
    except Exception:
        return None
