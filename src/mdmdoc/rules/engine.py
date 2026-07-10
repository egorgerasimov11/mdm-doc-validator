#!/usr/bin/env python3
"""
engine.py — declarative rule engine. Rules live in rules/*.yaml (editable, no
hidden model intuition). The engine iterates rules over the extraction, each
firing rule yields a Finding. It never crashes on a bad rule — it emits a
fail-closed `engine_error` finding (ENGINE-GUARD → NEED_MANUAL_REVIEW), so a
broken rule can hide a blocker but can never let a document silently ACCEPT.

`when` vocabulary:
  {always: true} | {field_missing: name} | {flag_true: name} | {flag_false: name}
  {equals: {field, value}} | {in: {field, values}} | {regex_mismatch: {field, pattern}}
  {check: <predicate>, field: name, args: {...}}
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import yaml

from .. import config
from ..fields import Extraction
from ..privacy import FIELD_KIND, mask
from .predicates import REGISTRY

SEVERITIES = ("CRITICAL", "WARNING", "NOTE")
VERDICTS = ("REJECT", "NEED_MANUAL_REVIEW", "WARNING", "ACCEPT")


@dataclass
class Finding:
    rule_id: str
    severity: str
    verdict_effect: str | None
    message: str
    detail: str = ""
    field: str = ""      # extraction field the rule fired on ('' when not field-bound)

    def to_dict(self) -> dict:
        return asdict(self)


def load_rules(doc_class: str) -> dict:
    from .. import rules_io
    return yaml.safe_load(rules_io.rules_text(doc_class)) or {}


def _field_str(flds: dict, name: str) -> str:
    v = flds.get(name, "")
    if isinstance(v, bool):
        return v
    return str(v or "").strip()


def _flag(flds: dict, name: str) -> bool:
    v = flds.get(name, False)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1", "x", "signed")


def _eval_when(when: dict, ext: Extraction, tables: dict) -> tuple[bool, str, str]:
    """-> (fired, detail, field_name_used)"""
    flds = ext.fields
    if when.get("always"):
        return True, "", ""
    if "field_missing" in when:
        f = when["field_missing"]
        return (not _field_str(flds, f), "", f)
    if "flag_true" in when:
        return (_flag(flds, when["flag_true"]), "", when["flag_true"])
    if "flag_false" in when:
        return (not _flag(flds, when["flag_false"]), "", when["flag_false"])
    if "equals" in when:
        spec = when["equals"]
        return (_field_str(flds, spec["field"]).lower() == str(spec["value"]).lower(), "", spec["field"])
    if "in" in when:
        spec = when["in"]
        return (_field_str(flds, spec["field"]).lower() in [str(v).lower() for v in spec["values"]],
                "", spec["field"])
    if "regex_mismatch" in when:
        import re
        spec = when["regex_mismatch"]
        v = _field_str(flds, spec["field"])
        if not v:
            return False, "", spec["field"]
        return (not re.match(spec["pattern"], v), "", spec["field"])
    if "check" in when:
        pred = REGISTRY.get(when["check"])
        if pred is None:
            raise KeyError(f"unknown predicate {when['check']!r}")
        fname = when.get("field", "")
        value = flds.get(fname, "") if fname else ""
        fired, detail = pred(value, flds, when.get("args", {}) or {}, tables)
        return fired, detail, fname
    raise KeyError(f"unrecognized when clause: {list(when.keys())}")


def run_rules(ext: Extraction, lang: str = "en", policy: str = "masked",
              enforce_approvals: bool = False,
              trace: list | None = None,
              pending_out: list | None = None) -> list[Finding]:
    """enforce_approvals=True is the HARD GATE (live pipeline): a rule fires only
    if a human Approved it in the panel. A rejected rule is skipped; a pending
    (never-reviewed or changed-since-approval) rule that APPLIES to the document
    does not fire but forces NEED_MANUAL_REVIEW so nothing silently ACCEPTs.
    Default False so eval/tests measure the raw rules (the machine, not the gate)."""
    cfg = load_rules(ext.doc_class)
    tables = cfg.get("tables", {}) or {}
    findings: list[Finding] = []
    approvals, pending_applicable = {}, []
    if enforce_approvals:
        from .. import rule_approvals
        approvals = rule_approvals.load()
    def _trace(rid, name, outcome, detail=""):
        # D6 reasoning trail: what happened to EVERY rule — fired / did not
        # fire / not applicable / skipped by the approval gate. Off (None) on
        # every existing call site; the pipeline passes a list for reasoning.md.
        if trace is not None:
            trace.append({"rule_id": rid, "name": name, "outcome": outcome,
                          "detail": str(detail)[:160]})

    for rule in cfg.get("rules", []) or []:
        rid = str(rule.get("id", "?"))
        rname = str(rule.get("name", ""))
        try:
            applies = rule.get("applies_to")
            if applies and ext.doc_type not in applies:
                _trace(rid, rname, "not-applicable",
                       f"applies_to {applies}, doc_type {ext.doc_type}")
                continue
            if enforce_approvals:
                from .. import rule_approvals
                st = rule_approvals.status(approvals, ext.doc_class, rule)
                if st != rule_approvals.APPROVED:
                    if st == rule_approvals.PENDING:
                        pending_applicable.append(
                            {"id": rid, "name": rname,
                             "source": str(rule.get("source") or ""),
                             "tier": str(rule.get("tier") or "")})
                    _trace(rid, rname, f"skipped-{st.lower()}",
                           "approval gate" if st == rule_approvals.PENDING
                           else "rejected by operator")
                    continue   # rejected -> silent skip; pending -> NMR below
            fired, detail, fname = _eval_when(rule.get("when", {}) or {}, ext, tables)
            if not fired:
                _trace(rid, rname, "did-not-fire")
                continue
            _trace(rid, rname,
                   f"FIRED {rule.get('severity', 'WARNING')}"
                   + (f" -> {rule.get('verdict_effect')}" if rule.get("verdict_effect") else ""),
                   detail)
            raw_value = str(ext.fields.get(fname, "") or "") if fname else ""
            kind = FIELD_KIND.get(fname)
            from ..privacy import display_value
            value_masked = display_value(kind, raw_value, policy) if kind and raw_value else raw_value
            msg_key = "message_ru" if lang == "ru" and rule.get("message_ru") else "message"
            msg = str(rule.get(msg_key, rule.get("message", ""))).format(
                value=value_masked if kind else raw_value, value_masked=value_masked, detail=detail)
            sev = rule.get("severity", "WARNING")
            eff = rule.get("verdict_effect")
            if eff is not None and eff not in VERDICTS:
                # A human approved a verdict effect the engine cannot honor —
                # dropping it silently would fail open. Hold for manual review.
                findings.append(Finding(
                    "ENGINE-GUARD", "WARNING", "NEED_MANUAL_REVIEW",
                    f"engine_error: rule {rid} declares invalid verdict_effect {eff!r} "
                    "— held for manual review"))
            findings.append(Finding(rid, sev if sev in SEVERITIES else "WARNING",
                                    eff if eff in VERDICTS else None, msg, detail,
                                    fname or ""))
        except Exception as e:
            # Fail closed: an errored rule might have been a REJECT/NMR. The
            # document is held for a human instead of silently passing.
            findings.append(Finding(
                "ENGINE-GUARD", "WARNING", "NEED_MANUAL_REVIEW",
                f"engine_error: rule {rid} failed ({e.__class__.__name__}: {e}) "
                "— fail-closed: held for manual review"))
    if pending_applicable:
        if pending_out is not None:
            pending_out.extend(pending_applicable)

        def _tag(p):
            bits = ", ".join(x for x in (p["source"], p["tier"]) if x)
            return p["id"] + (f" ({bits})" if bits else "")

        shown = ", ".join(_tag(p) for p in pending_applicable[:8]) \
            + ("…" if len(pending_applicable) > 8 else "")
        findings.insert(0, Finding(
            "RULE-GATE", "WARNING", "NEED_MANUAL_REVIEW",
            f"{len(pending_applicable)} rule(s) that apply to this document await your "
            f"approval ({shown}) — approve them under Rules → Approvals. The verdict is "
            "not trusted until every applicable rule is approved."))
    return findings
