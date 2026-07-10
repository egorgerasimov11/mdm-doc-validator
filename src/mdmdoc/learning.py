#!/usr/bin/env python3
"""
learning.py — the note-to-rule channel (D11b): an operator's free-text label
NOTE stops being a dead-end. On submit it is classified by rule_propose; a
clean rule proposal that only ADDS a rule is auto-appended to the rules file
as PENDING (source operator, tier learned) — the approval hash gate keeps the
verdict untouched until Egor approves it in the panel. Everything else lands
in rules/proposals.jsonl for the Training page.

Safety: an auto-apply may NEVER modify or remove an existing rule — that would
flip approved rules back to pending and blanket-NMR their doc types. Only pure
additions pass; the rest is queued for a human.
"""
from __future__ import annotations

import json
import threading

import yaml

from . import config, rule_propose, rules_io
from .rule_approvals import rule_hash

PROPOSALS_NAME = "proposals.jsonl"
_LOCK = threading.Lock()


def _proposals_path():
    return config.RULES_DIR / PROPOSALS_NAME


def _queue_proposal(run_id: str, note: str, prop: dict, reason: str) -> None:
    row = {"run_id": run_id, "note": note[:500], "kind": prop.get("kind"),
           "rationale": str(prop.get("rationale", ""))[:300],
           "reason_queued": reason,
           "doc_class": prop.get("doc_class", ""),
           "rule_id": prop.get("rule_id", "")}
    with _LOCK:
        config.RULES_DIR.mkdir(parents=True, exist_ok=True)
        with open(_proposals_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_proposals() -> list[dict]:
    p = _proposals_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _only_adds_rules(cur_yaml: str, proposed_yaml: str) -> bool:
    """True when every EXISTING rule survives byte-hash-identical and at least
    one new rule id appears — the only shape safe to auto-apply."""
    try:
        cur = yaml.safe_load(cur_yaml) or {}
        new = yaml.safe_load(proposed_yaml) or {}
        cur_rules = {str(r.get("id")): r for r in cur.get("rules", [])
                     if isinstance(r, dict)}
        new_rules = {str(r.get("id")): r for r in new.get("rules", [])
                     if isinstance(r, dict)}
    except Exception:
        return False
    if not set(cur_rules) <= set(new_rules):
        return False                       # something removed
    for rid, rule in cur_rules.items():
        if rule_hash(rule) != rule_hash(new_rules[rid]):
            return False                   # something modified
    return len(new_rules) > len(cur_rules)


def _stamp_governance(proposed_yaml: str, cur_yaml: str) -> str:
    """Ensure the ADDED rules carry source: operator + tier: learned (the
    proposal model may omit governance metadata)."""
    try:
        cur_ids = {str(r.get("id")) for r in (yaml.safe_load(cur_yaml) or {}).get("rules", [])
                   if isinstance(r, dict)}
        doc = yaml.safe_load(proposed_yaml) or {}
        changed = False
        for r in doc.get("rules", []) or []:
            if isinstance(r, dict) and str(r.get("id")) not in cur_ids:
                if not r.get("source"):
                    r["source"] = "operator"
                    changed = True
                if not r.get("tier"):
                    r["tier"] = "learned"
                    changed = True
        if changed:
            return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    except Exception:
        pass
    return proposed_yaml


def note_to_rule(run_id: str, note: str, log=print) -> dict:
    """The background job body. Returns a summary dict for the job result."""
    log(f"classifying the note via the strong model…")
    prop = rule_propose.propose(run_id, note)
    kind = prop.get("kind")
    if kind != "rule":
        _queue_proposal(run_id, note, prop, f"kind={kind}")
        log(f"note classified as '{kind}' — queued in rules/proposals.jsonl")
        return {"queued": kind}
    if prop.get("validation") or not prop.get("applicable"):
        _queue_proposal(run_id, note, prop, "validation issues")
        log("rule proposal has validation issues — queued for manual review")
        return {"queued": "invalid-rule"}
    cur, proposed = prop.get("current_yaml", ""), prop.get("proposed_yaml", "")
    if not _only_adds_rules(cur, proposed):
        _queue_proposal(run_id, note, prop, "modifies existing rules")
        log("proposal MODIFIES existing rules — auto-apply is addition-only; "
            "queued for manual review (apply it from the run page if intended)")
        return {"queued": "modifies-existing"}
    proposed = _stamp_governance(proposed, cur)
    rules_io.save_rules(prop["doc_class"], proposed)
    log(f"new rule appended to {prop['doc_class']} rules as PENDING "
        f"(source operator, tier learned) — approve it under Rules → Approvals")
    return {"applied_pending": prop.get("rule_id", "")}
