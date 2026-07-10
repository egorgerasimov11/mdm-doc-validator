#!/usr/bin/env python3
"""
rule_approvals.py — human sign-off gate for every document rule.

HARD GATE (Egor's decision): a rule fires ONLY when a human has Approved it in
the panel. This is the write choke point for `rules/approvals.json` — Egor's
per-rule decisions ({approved|rejected} + the content hash of the rule he saw).

Status of a rule against the current YAML:
  * approved — decision "approved" AND the rule's content hash still matches what
               was approved (so an edited rule auto-reverts to pending).
  * rejected — Egor disabled the exact rule text he saw → it never fires, no
               review flag. An edited rejected rule reverts to pending (a
               repurposed rule id must never inherit an old rejection).
  * pending  — never reviewed, OR reviewed earlier (approved OR rejected) but
               the rule text changed since.

The engine (rules/engine.py) consults this: approved rules run normally; a
pending rule that APPLIES to the document does not fire but forces the run to
NEED_MANUAL_REVIEW (so nothing silently ACCEPTs on an un-approved rule); a
rejected rule is skipped silently.

`rules/approvals.json` carries NO PII (rule ids + hashes + notes) and is NOT
copied to the ABAP side (regenerate copies only banking.yaml/w9.yaml). Keep it
out of the deploy rsync so the mini's live decisions are never clobbered.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading

from . import config

APPROVED, REJECTED, PENDING = "approved", "rejected", "pending"

# serializes read-modify-write of the ledger across the server threadpool
_LOCK = threading.Lock()
_log = logging.getLogger("mdmdoc.rule_approvals")

# Keys that are governance METADATA, not verdict behaviour. They are excluded
# from the content hash so annotating a rule with tier/source does NOT invalidate
# an existing human approval (a denylist keeps the hash byte-identical for every
# rule that predates the metadata — zero migration of the mini's approvals.json).
_METADATA_KEYS = ("tier", "source")


def _path():
    return config.RULES_DIR / "approvals.json"


def rule_hash(rule: dict) -> str:
    """Stable content hash of a rule block — approval is bound to exact VERDICT
    content, not to governance metadata (tier/source). Adding/changing metadata
    leaves the hash unchanged; changing id/when/severity/verdict_effect/message
    (anything that alters what the rule DOES) re-invalidates the approval."""
    verdict_body = {k: v for k, v in rule.items() if k not in _METADATA_KEYS}
    blob = json.dumps(verdict_body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _key(doc_class: str, rule_id: str) -> str:
    return f"{doc_class}:{rule_id}"


def load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # The human-approval ledger is unreadable. Fail CLOSED (empty store =
        # every rule PENDING -> RULE-GATE NMR, nothing silently fires), but do
        # it LOUDLY and keep the first corrupt snapshot for forensics — the
        # operator's decisions must never vanish without a trace.
        _log.error("approvals.json is corrupt (%s: %s) — treating every rule "
                   "as PENDING; original bytes preserved at approvals.json.corrupt",
                   e.__class__.__name__, e)
        backup = p.with_name(p.name + ".corrupt")
        if not backup.exists():
            try:
                shutil.copy2(p, backup)
            except OSError:
                pass
        return {}


def status(store: dict, doc_class: str, rule: dict) -> str:
    """Current gate status of `rule` given the saved decisions `store`."""
    entry = store.get(_key(doc_class, str(rule.get("id", ""))))
    if not entry:
        return PENDING
    if entry.get("status") == REJECTED and entry.get("hash") == rule_hash(rule):
        return REJECTED
    if entry.get("status") == APPROVED and entry.get("hash") == rule_hash(rule):
        return APPROVED
    return PENDING   # reviewed earlier but the rule text changed → re-review


def set_decision(doc_class: str, rule: dict, decision: str, note: str = "",
                 by: str = "egor") -> dict:
    """Record Approve/Reject for one rule. Returns the updated store."""
    if decision not in (APPROVED, REJECTED, PENDING):
        raise ValueError(f"decision must be approved/rejected/pending, got {decision!r}")
    with _LOCK:
        store = load()
        if decision == PENDING:
            store.pop(_key(doc_class, str(rule.get("id", ""))), None)
        else:
            store[_key(doc_class, str(rule.get("id", "")))] = {
                "status": decision, "hash": rule_hash(rule), "note": note, "by": by,
                "ts": __import__("mdmdoc.runstore", fromlist=["now_iso"]).now_iso()}
        config.ensure_dirs()
        config.atomic_write_text(_path(),
                                 json.dumps(store, indent=2, ensure_ascii=False))
    return store


def clear_entry(doc_class: str, rule_id: str) -> None:
    """Drop a rule's ledger entry entirely (E6 delete): a future rule reusing
    the id starts life PENDING instead of inheriting a stale decision."""
    with _LOCK:
        store = load()
        if store.pop(_key(doc_class, rule_id), None) is not None:
            config.ensure_dirs()
            config.atomic_write_text(_path(),
                                     json.dumps(store, indent=2, ensure_ascii=False))


def summarize(doc_class: str, rules: list) -> dict:
    """Counts + per-rule status for the panel."""
    store = load()
    rows, counts = [], {APPROVED: 0, REJECTED: 0, PENDING: 0}
    for r in rules:
        st = status(store, doc_class, r)
        counts[st] += 1
        entry = store.get(_key(doc_class, str(r.get("id", "")))) or {}
        rows.append({"id": r.get("id"), "status": st, "note": entry.get("note", ""),
                     "ts": entry.get("ts", "")})
    return {"counts": counts, "rows": rows}
