#!/usr/bin/env python3
"""
report.py — render the human report (skill-format blocks) and the machine JSON.
Everything rendered here is already masked (extraction.to_public()), and the
leak gate runs again on the final artifacts in runstore.write().
"""
from __future__ import annotations

import json

from jinja2 import Environment, FileSystemLoader

from . import config
from .fields import Extraction
from .rules.engine import Finding
from .verdict import next_step

_env = Environment(loader=FileSystemLoader(str(config.TEMPLATES_DIR)),
                   trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)


def _group(findings: list[Finding]) -> dict:
    return {
        "critical": [f for f in findings if f.severity == "CRITICAL"],
        "warnings": [f for f in findings if f.severity == "WARNING"],
        "notes": [f for f in findings if f.severity == "NOTE"],
    }


def _evidence(pub: dict) -> list[str]:
    """Short masked evidence lines: which identity elements the document shows."""
    f = pub.get("fields", {})
    ev = []
    if pub["doc_class"] == "bank":
        if f.get("bank_name"):
            ev.append(f"bank: {f['bank_name']}")
        if f.get("account_holder"):
            ev.append(f"holder: {f['account_holder']}")
        for k, label in (("iban", "IBAN"), ("account_number", "account"), ("routing_aba", "routing")):
            v = f.get(k)
            if isinstance(v, dict) and v.get("present"):
                ev.append(f"{label}: {v['masked']}")
        if f.get("swift_bic"):
            ev.append(f"SWIFT: {f['swift_bic']}")
        if isinstance(f.get("signed"), bool):
            ev.append("signed/stamped: " + ("yes" if f["signed"] else "no"))
    else:
        if f.get("line1_name"):
            ev.append(f"Line 1: {f['line1_name']}")
        if f.get("line2_business_name"):
            ev.append(f"Line 2: {f['line2_business_name']}")
        tin = f.get("tin", {})
        if tin.get("present"):
            ev.append(f"TIN ({tin.get('type', '?')}): {tin.get('masked')}")
    return ev


def render_report(pub: dict, findings: list[Finding], verdict: str, lang: str = "en") -> str:
    tpl = _env.get_template("report_bank.md.j2" if pub["doc_class"] == "bank" else "report_w9.md.j2")
    groups = _group(findings)
    why = (groups["critical"][0].message if groups["critical"]
           else groups["warnings"][0].message if groups["warnings"]
           else "Document shows sufficient banking identity for support."
           if pub["doc_class"] == "bank" else "Form is complete and internally consistent.")
    return tpl.render(pub=pub, fields=pub.get("fields", {}), groups=groups, why=why,
                      verdict=verdict, evidence=_evidence(pub),
                      next_step=next_step(pub["doc_class"], verdict), lang=lang)


def build_json(pub: dict, findings: list[Finding], verdict: str, meta: dict) -> str:
    obj = {
        "schema": "mdmdoc.v1",
        "doc": meta.get("path"),
        "run_id": meta.get("run_id"),
        "doc_class": pub["doc_class"],
        "doc_type": pub["doc_type"],
        "verdict": verdict,
        "next_step": next_step(pub["doc_class"], verdict),
        "findings": [f.to_dict() for f in findings],
        "fields": pub.get("fields", {}),
        "crosscheck": pub.get("crosscheck", []),
        "warnings": pub.get("warnings", []),
        "sensitive_present": pub.get("sensitive_present", {}),
        "model": pub.get("model"),
        "json_valid_first_try": pub.get("json_valid_first_try"),
        "ts": meta.get("ts"),
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)
