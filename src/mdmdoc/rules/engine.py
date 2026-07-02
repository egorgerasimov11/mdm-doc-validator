#!/usr/bin/env python3
"""
engine.py — declarative rule engine. Rules live in rules/*.yaml (editable, no
hidden model intuition). The engine iterates rules over the extraction, each
firing rule yields a Finding. It never crashes on a bad rule — it emits an
`engine_error` finding instead.

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

    def to_dict(self) -> dict:
        return asdict(self)


def load_rules(doc_class: str) -> dict:
    p = config.RULES_DIR / ("banking.yaml" if doc_class == "bank" else "w9.yaml")
    return yaml.safe_load(p.read_text()) or {}


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


def run_rules(ext: Extraction, lang: str = "en") -> list[Finding]:
    cfg = load_rules(ext.doc_class)
    tables = cfg.get("tables", {}) or {}
    findings: list[Finding] = []
    for rule in cfg.get("rules", []) or []:
        rid = str(rule.get("id", "?"))
        try:
            applies = rule.get("applies_to")
            if applies and ext.doc_type not in applies:
                continue
            fired, detail, fname = _eval_when(rule.get("when", {}) or {}, ext, tables)
            if not fired:
                continue
            raw_value = str(ext.fields.get(fname, "") or "") if fname else ""
            kind = FIELD_KIND.get(fname)
            value_masked = mask(kind, raw_value) if kind and raw_value else raw_value
            msg_key = "message_ru" if lang == "ru" and rule.get("message_ru") else "message"
            msg = str(rule.get(msg_key, rule.get("message", ""))).format(
                value=value_masked if kind else raw_value, value_masked=value_masked, detail=detail)
            sev = rule.get("severity", "WARNING")
            eff = rule.get("verdict_effect")
            findings.append(Finding(rid, sev if sev in SEVERITIES else "WARNING",
                                    eff if eff in VERDICTS else None, msg, detail))
        except Exception as e:
            findings.append(Finding(rid, "NOTE", None,
                                    f"engine_error: rule {rid} failed ({e.__class__.__name__}: {e})"))
    return findings
