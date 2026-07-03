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


def _fmt(v, empty="—") -> str:
    if isinstance(v, dict):
        if not v.get("present"):
            return empty
        # 'value' present only under the full display policy (banking kinds);
        # TIN dicts never carry it — they fall back to the mask
        return v.get("value") or v.get("masked") or empty
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v).strip() or empty


def _data_rows(pub: dict) -> list[tuple[str, str, str]]:
    """(label, value, sap-hint) rows for the EXTRACTED DATA block."""
    f = pub.get("fields", {})
    if pub["doc_class"] == "bank":
        iban = f.get("iban")
        iban_extra = (f" (country {iban['country']}, len {iban['length']})"
                      if isinstance(iban, dict) and iban.get("present") and iban.get("country") else "")
        signed = "yes" if f.get("signed") else "no"
        if not f.get("signed") and f.get("signature_evidence"):
            signed = f"no ({f['signature_evidence']})"
        return [
            ("Account holder", _fmt(f.get("account_holder")), ""),
            ("Account type", _fmt(f.get("account_type")), ""),
            ("Bank name", _fmt(f.get("bank_name")), ""),
            ("Bank country", _fmt(f.get("bank_country")), ""),
            ("Bank address", _fmt(f.get("bank_address")), ""),
            ("IBAN", _fmt(iban) + iban_extra, ""),
            ("SWIFT/BIC", _fmt(f.get("swift_bic")), ""),
            ("Account number", _fmt(f.get("account_number")), ""),
            ("Routing/ABA (standard)", _fmt(f.get("routing_aba")), ""),
            ("Routing/ABA (wires)", _fmt(f.get("routing_aba_wires")), ""),
            ("Currency", _fmt(f.get("currency")), ""),
            ("Document date", _fmt(f.get("doc_date"), empty="no visible document date"), ""),
            ("Signed/stamped", signed, ""),
        ]
    tin = f.get("tin", {}) if isinstance(f.get("tin"), dict) else {}
    tin_target = ("Tax Number 1" if tin.get("type") == "SSN"
                  else "Tax Number 2" if tin.get("type") == "EIN" else "")
    signed = _fmt(f.get("signed", False))
    if f.get("sign_date"):
        signed += f" ({f['sign_date']})"
    return [
        ("Line 1 — taxpayer name", _fmt(f.get("line1_name")), "SAP Name 1"),
        ("Line 2 — business name", _fmt(f.get("line2_business_name")), "SAP Name 2"),
        ("Classification (Line 3)", _fmt(f.get("line3_classification")), ""),
        ("TIN type", tin.get("type") or "—", tin_target),
        ("TIN (masked)", _fmt(tin), ""),
        ("Address — street", _fmt(f.get("address_street")), ""),
        ("Address — city/state/ZIP", _fmt(f.get("address_city_state_zip")), ""),
        ("Signed", signed, ""),
    ]


def data_block(pub: dict) -> str:
    rows = _data_rows(pub)
    w = max(len(r[0]) for r in rows)
    lines = ["─" * 18 + " EXTRACTED DATA " + "─" * 18]
    for label, value, hint in rows:
        line = f"{label:<{w}} : {value}"
        if hint and value != "—":
            line += f"   → {hint}"
        lines.append(line)
    lines.append("─" * 52)
    return "\n".join(lines)


def sap_block(rows: list[dict]) -> str:
    """Masked doc-vs-SAP comparison table for the text report."""
    if not rows:
        return ""
    wf = max(len(r["field"]) for r in rows)
    wd = max(max(len(r["doc"]) for r in rows), 8)
    ws = max(max(len(r["sap"]) for r in rows), 8)
    lines = ["─" * 16 + " SAP COMPARISON " + "─" * 16,
             f"{'field':<{wf}}   {'document':<{wd}}   {'SAP':<{ws}}   result"]
    for r in rows:
        res = r["status"] + (f" ({r['note']})" if r.get("note") else "")
        lines.append(f"{r['field']:<{wf}}   {r['doc']:<{wd}}   {r['sap']:<{ws}}   {res}")
    lines.append("─" * 48)
    return "\n".join(lines)


def render_report(pub: dict, findings: list[Finding], verdict: str, lang: str = "en") -> str:
    tpl = _env.get_template("report_bank.md.j2" if pub["doc_class"] == "bank" else "report_w9.md.j2")
    groups = _group(findings)
    why = (groups["critical"][0].message if groups["critical"]
           else groups["warnings"][0].message if groups["warnings"]
           else "Document shows sufficient banking identity for support."
           if pub["doc_class"] == "bank" else "Form is complete and internally consistent.")
    return tpl.render(pub=pub, fields=pub.get("fields", {}), groups=groups, why=why,
                      verdict=verdict, evidence=_evidence(pub), data_block=data_block(pub),
                      sap_block=sap_block(pub.get("sap_compare", [])),
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
        "sap_compare": pub.get("sap_compare", []),
        "warnings": pub.get("warnings", []),
        "sensitive_present": pub.get("sensitive_present", {}),
        "model": pub.get("model"),
        "json_valid_first_try": pub.get("json_valid_first_try"),
        "ts": meta.get("ts"),
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)
