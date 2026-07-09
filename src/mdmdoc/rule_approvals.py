#!/usr/bin/env python3
"""
rule_approvals.py — human sign-off gate for every document rule.

HARD GATE (Egor's decision): a rule fires ONLY when a human has Approved it in
the panel. This is the write choke point for `rules/approvals.json` — Egor's
per-rule decisions ({approved|rejected} + the content hash of the rule he saw).

Status of a rule against the current YAML:
  * approved — decision "approved" AND the rule's content hash still matches what
               was approved (so an edited rule auto-reverts to pending).
  * rejected — Egor disabled the rule on purpose → it never fires, no review flag.
  * pending  — never reviewed, OR approved earlier but the rule text changed.

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

from . import config

APPROVED, REJECTED, PENDING = "approved", "rejected", "pending"

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
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def status(store: dict, doc_class: str, rule: dict) -> str:
    """Current gate status of `rule` given the saved decisions `store`."""
    entry = store.get(_key(doc_class, str(rule.get("id", ""))))
    if not entry:
        return PENDING
    if entry.get("status") == REJECTED:
        return REJECTED
    if entry.get("status") == APPROVED and entry.get("hash") == rule_hash(rule):
        return APPROVED
    return PENDING   # approved earlier but the rule text changed → re-approve


def set_decision(doc_class: str, rule: dict, decision: str, note: str = "",
                 by: str = "egor") -> dict:
    """Record Approve/Reject for one rule. Returns the updated store."""
    if decision not in (APPROVED, REJECTED, PENDING):
        raise ValueError(f"decision must be approved/rejected/pending, got {decision!r}")
    store = load()
    if decision == PENDING:
        store.pop(_key(doc_class, str(rule.get("id", ""))), None)
    else:
        store[_key(doc_class, str(rule.get("id", "")))] = {
            "status": decision, "hash": rule_hash(rule), "note": note, "by": by,
            "ts": __import__("mdmdoc.runstore", fromlist=["now_iso"]).now_iso()}
    config.ensure_dirs()
    _path().write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    return store


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
