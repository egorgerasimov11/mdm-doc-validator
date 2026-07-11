#!/usr/bin/env python3
"""
undo.py — the operator's "Отозвать" button (F1): every mutating action in the
oplog can be taken back, from one place, with the same safety story everywhere.

Design: the oplog row (keyed by its `op` id) carries what the action changed
("prev -> new" details, backup basenames); this module holds one handler per
action that (1) refuses when the state has MOVED ON since the row (409-style
StaleState — undoing yesterday's decision must not clobber today's), (2)
refuses a second undo of the same op, (3) performs the revert through the
existing choke points (rules_io / rule_approvals / dataset / patterns /
challenges / ratings — this module writes NO files itself), and (4) logs one
`undo` row referencing the op. Non-state actions (check, jobs, train, eval)
are simply not undoable — there is nothing to revert.
"""
from __future__ import annotations

from . import challenges, config, dataset, oplog, patterns, ratings, rule_approvals, rules_io


class UndoError(Exception):
    """Cannot undo — with an operator-readable reason."""


class StaleState(UndoError):
    """The state moved on after this action — undo would clobber newer work."""


# actions this module knows how to revert (the History page shows the button
# for these rows only, and only while no undo row references them)
UNDOABLE = ("rule-delete", "rule-approve", "rule-tier", "rule-save",
            "rule-restore", "rule-create",
            "label", "mark-valid", "teach-type", "rating", "finding-vote")

# whole-file writers: any of these NEWER than a rule-save row means its
# pre-write snapshot no longer represents "just before the latest change"
_FILE_WRITERS = ("rule-save", "rule-tier", "rule-delete", "rule-restore",
                 "rule-create", "skill-upload")

_LABEL_FAMILY = ("label", "mark-valid", "teach-type")


def _all_rows() -> list[dict]:
    """Newest first, full ledger."""
    return oplog.recent(limit=0)


def undone_ops(rows: list[dict] | None = None) -> set[str]:
    """op ids already consumed by an undo row (detail starts with the op)."""
    rows = rows if rows is not None else _all_rows()
    out = set()
    for r in rows:
        if r.get("action") == "undo" and r.get("detail"):
            out.add(str(r["detail"]).split()[0])
    return out


def can_undo(row: dict, undone: set[str]) -> bool:
    """Cheap UI-level check: is the Undo button worth showing? (Deep state
    checks run at perform time.)"""
    action = row.get("action", "")
    if action not in UNDOABLE or not row.get("op") or row["op"] in undone:
        return False
    detail = str(row.get("detail") or "")
    if action == "rule-delete":
        return detail.startswith("backup ")
    if action in ("rule-approve", "rule-tier"):
        return " -> " in detail and not detail.startswith("?")
    if action == "rating":
        return detail in ("up", "down")
    if action in _LABEL_FAMILY:
        return bool(row.get("run_id"))
    if action == "finding-vote":
        return bool(row.get("rule_id") and row.get("run_id"))
    return True


def _newer_rows(rows: list[dict], op: str) -> list[dict]:
    """Rows appended AFTER the op's row (ledger order beats second-precision ts)."""
    out: list[dict] = []
    for r in rows:                      # rows are newest first
        if r.get("op") == op:
            return out
        out.append(r)
    raise UndoError(f"action {op} not found in the history ledger")


def _load_rule(doc_class: str, rule_id: str) -> dict | None:
    import yaml
    cfg = yaml.safe_load(rules_io.rules_text(doc_class) or "") or {}
    for r in cfg.get("rules", []) or []:
        if isinstance(r, dict) and str(r.get("id")) == rule_id:
            return r
    return None


def _parse_transition(detail: str) -> tuple[str, str]:
    prev, _, new = detail.partition(" -> ")
    prev, new = prev.strip(), new.strip()
    if not prev or not new:
        raise UndoError(f"this history row does not carry the previous value ({detail!r})")
    return prev, new


# ---------------------------------------------------------------- handlers ----
def _undo_rule_delete(row: dict, rows: list[dict]) -> dict:
    doc_class = row.get("doc_class", "bank")
    rule_id = row.get("rule_id", "")
    backup = str(row.get("detail", "")).removeprefix("backup ").strip()
    if _load_rule(doc_class, rule_id):
        raise StaleState(f"a rule {rule_id} exists again in the {doc_class} rules — "
                         "nothing to restore")
    out = rules_io.restore_rule(doc_class, backup)
    return {"restored": out["rule_id"], "doc_class": doc_class,
            "note": "the rule is back as PENDING (its approval was cleared at delete "
                    "time) and its old challenges stay dismissed — approve it in the panel"}


def _undo_rule_approve(row: dict, rows: list[dict]) -> dict:
    doc_class = row.get("doc_class", "bank")
    rule_id = row.get("rule_id", "")
    prev, new = _parse_transition(str(row.get("detail", "")))
    rule = _load_rule(doc_class, rule_id)
    if rule is None:
        raise StaleState(f"rule {rule_id} no longer exists in the {doc_class} rules")
    current = rule_approvals.status(rule_approvals.load(), doc_class, rule)
    if current != new:
        raise StaleState(f"{rule_id} is {current} now (this action set {new}) — "
                         "decide it in the panel instead")
    rule_approvals.set_decision(doc_class, rule, prev)
    note = ""
    if new == "rejected":
        note = "the challenges auto-dismissed by the rejection stay dismissed"
    return {"rule_id": rule_id, "decision": prev, "note": note}


def _undo_rule_tier(row: dict, rows: list[dict]) -> dict:
    doc_class = row.get("doc_class", "bank")
    rule_id = row.get("rule_id", "")
    old, new = _parse_transition(str(row.get("detail", "")))
    if old in ("?", "—"):
        raise UndoError(f"{rule_id} had no tier before this change — set one in the panel")
    rule = _load_rule(doc_class, rule_id)
    if rule is None:
        raise StaleState(f"rule {rule_id} no longer exists in the {doc_class} rules")
    if str(rule.get("tier") or "") != new:
        raise StaleState(f"{rule_id} tier is {rule.get('tier')!r} now (this action set "
                         f"{new!r}) — change it in the panel instead")
    rules_io.set_rule_tier(doc_class, rule_id, old)
    return {"rule_id": rule_id, "tier": old}


def _undo_rule_save(row: dict, rows: list[dict]) -> dict:
    newer = [r for r in _newer_rows(rows, row["op"])
             if r.get("action") in _FILE_WRITERS]
    if newer:
        n = newer[-1]   # the OLDEST newer writer — the first thing that moved on
        raise StaleState(f"the rules file changed again after this save "
                         f"({n.get('action')} at {n.get('ts')}) — undo refused; "
                         "restore a specific version from rules/history/ instead")
    compact = str(row.get("ts", "")).replace(":", "").replace("-", "").rstrip("Z")
    snaps = [s for s in rules_io.list_snapshots()
             if s.removeprefix("rules-").rsplit("-", 1)[0] <= compact]
    if not snaps:
        raise UndoError("no pre-save snapshot exists for this save (rules/history/ "
                        "starts after it)")
    out = rules_io.restore_snapshot(snaps[0])
    return {"restored_snapshot": out["snapshot"],
            "note": "edited rules re-pend automatically where content differs"}


def _undo_rule_created(row: dict, rows: list[dict]) -> dict:
    """Undo of rule-restore / rule-create: the rule was ADDED — remove it."""
    doc_class = row.get("doc_class", "bank")
    rule_id = row.get("rule_id", "")
    if not _load_rule(doc_class, rule_id):
        raise StaleState(f"rule {rule_id} is not in the {doc_class} rules anymore")
    out = rules_io.delete_rule(doc_class, rule_id)
    return {"deleted": rule_id, "backup": out["backup"]}


def _undo_label(row: dict, rows: list[dict]) -> dict:
    run_id = row.get("run_id", "")
    newer = [r for r in _newer_rows(rows, row["op"])
             if r.get("run_id") == run_id and r.get("action") in _LABEL_FAMILY]
    if newer:
        raise StaleState(f"the document was labeled again after this action "
                         f"({newer[-1].get('action')} at {newer[-1].get('ts')}) — "
                         "undo that newer action first")
    removed = dataset.delete_label(run_id)
    if removed is None:
        raise StaleState("no label exists for this document anymore")
    patterns.drop(run_id, removed.get("ts", ""))
    # challenges this valid-mark filed (E4) are taken back one by one
    retracted = []
    if row.get("action") in ("mark-valid", "teach-type"):
        for ch in challenges.load():
            if ch.get("run_id") == run_id and ch.get("kind") == "valid-mark":
                challenges.retract(ch.get("rule_id", ""), run_id,
                                   note=f"undo {row['op']}")
                retracted.append(ch.get("rule_id", ""))
    try:                                # doc-type profile (F5) — soft until it ships
        from . import doctype_profiles
        doctype_profiles.drop(run_id)
    except ImportError:
        pass
    restored = None
    prev = dataset.last_replaced_label(run_id, replaced_ts=removed.get("ts", ""))
    if prev is not None:
        try:
            dataset.append_label(prev)   # leak gate re-runs; fail-closed on error
            restored = prev.get("ts", "")
        except Exception as e:  # noqa: BLE001
            raise UndoError(f"label removed, but its predecessor failed the leak "
                            f"gate and was NOT restored: {e}")
    # keep the prompt honest: the deleted label must leave the few-shot set now
    from .fewshot import build_fewshot
    try:
        build_fewshot()
    except Exception:
        pass
    # be honest about what remains: restoring a predecessor label can bring
    # back an EARLIER precedent (e.g. undoing a Mark valid rolls back to a
    # correction that ALSO set the verdict) — claiming "the precedent is gone"
    # then misleads the operator when OPERATOR-2 keeps firing.
    if restored and prev is not None:
        relaxing_left = bool(prev.get("verdict_gold")) and not prev.get("verdict_confirmed")
        note = ("undone — the PREVIOUS label was restored, so this document still "
                "has an operator precedent"
                + (f" (verdict {prev.get('verdict_gold')}, unconfirmed — this is what "
                   "OPERATOR-2 flags; teach the type again or clear the verdict to drop it)"
                   if relaxing_left else "")
                + ". Re-analyze to refresh the stored verdict.")
    else:
        note = "the precedent is gone; re-analyze the document to refresh its stored verdict."
    return {"label_removed": run_id, "label_restored_ts": restored,
            "challenges_retracted": sorted(set(filter(None, retracted))),
            "note": note}


def _undo_rating(row: dict, rows: list[dict]) -> dict:
    run_id = row.get("run_id", "")
    was = str(row.get("detail", ""))
    current = ratings.latest().get(run_id, "")
    if current != was:
        raise StaleState(f"the run is rated {current or 'nothing'} now (this action "
                         f"set {was}) — rate it again instead")
    ratings.record(run_id, "")
    try:
        from . import doctype_profiles
        if was == "up":
            doctype_profiles.drop(run_id, source="rated-up")
    except ImportError:
        pass
    return {"run_id": run_id, "rating_cleared": was}


def _undo_finding_vote(row: dict, rows: list[dict]) -> dict:
    rule_id, run_id = row.get("rule_id", ""), row.get("run_id", "")
    if challenges.counts().get(rule_id, 0) <= 0:
        raise StaleState(f"{rule_id} has no live challenges to take back")
    challenges.retract(rule_id, run_id, note=f"undo {row['op']}")
    return {"rule_id": rule_id, "challenges": challenges.counts().get(rule_id, 0)}


_HANDLERS = {
    "rule-delete": _undo_rule_delete,
    "rule-approve": _undo_rule_approve,
    "rule-tier": _undo_rule_tier,
    "rule-save": _undo_rule_save,
    "rule-restore": _undo_rule_created,
    "rule-create": _undo_rule_created,
    "label": _undo_label,
    "mark-valid": _undo_label,
    "teach-type": _undo_label,
    "rating": _undo_rating,
    "finding-vote": _undo_finding_vote,
}


def perform(op: str) -> dict:
    """Undo the action identified by its oplog `op` id. Raises UndoError /
    StaleState with operator-readable reasons; logs one `undo` row on success."""
    rows = _all_rows()
    row = next((r for r in rows if r.get("op") == op), None)
    if row is None:
        raise UndoError(f"no history row with id {op}")
    action = row.get("action", "")
    handler = _HANDLERS.get(action)
    if handler is None:
        raise UndoError(f"'{action}' actions cannot be undone — nothing to revert")
    if op in undone_ops(rows):
        raise StaleState("this action was already undone")
    result = handler(row, rows)
    oplog.log("undo", run_id=row.get("run_id", ""), rule_id=row.get("rule_id", ""),
              doc_class=row.get("doc_class", ""),
              detail=f"{op} {action}: " + (result.get("note") or "reverted"))
    return {"ok": True, "op": op, "action": action, **result}
