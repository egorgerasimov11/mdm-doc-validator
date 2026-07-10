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


def _is_w8_pub(pub: dict) -> bool:
    """W-8 subtype detection by SCHEMA, not just doc_type: only a run that was
    actually extracted with W8_KEYS gets the W-8 report — a model-guessed 'w8'
    over the legacy W-9 schema falls back to the W-9 report (W9-030 covers)."""
    return (pub.get("doc_class") == "w9" and pub.get("doc_type") == "w8"
            and "legal_name" in (pub.get("fields") or {}))


def _evidence(pub: dict) -> list[str]:
    """Short masked evidence lines: which identity elements the document shows."""
    f = pub.get("fields", {})
    ev = []
    if _is_w8_pub(pub):
        if f.get("form_variant"):
            ev.append(f"form: {f['form_variant']}")
        if f.get("legal_name"):
            ev.append(f"beneficial owner: {f['legal_name']}")
        if f.get("country_incorporation"):
            ev.append(f"country: {f['country_incorporation']}")
        ftin = f.get("foreign_tin", {})
        if isinstance(ftin, dict) and ftin.get("present"):
            ev.append(f"foreign TIN: {ftin.get('masked')}")
        if isinstance(f.get("signed"), bool):
            ev.append("signed: " + ("yes" if f["signed"] else "no"))
        return ev
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


def _data_rows(pub: dict) -> list[tuple[str, str, str, str]]:
    """(label, value, sap-hint, field-key) rows for the EXTRACTED DATA block.
    The field key matches pub['fields'] / pub['provenance'] — the run page uses
    it to attach source tags and evidence crops."""
    f = pub.get("fields", {})
    if pub["doc_class"] == "bank":
        iban = f.get("iban")
        iban_extra = (f" (country {iban['country']}, len {iban['length']})"
                      if isinstance(iban, dict) and iban.get("present") and iban.get("country") else "")
        kind = str(f.get("signature_kind") or "")
        signed = "yes" if f.get("signed") else "no"
        if f.get("signed") and kind in ("wet", "electronic", "stamp"):
            signed = f"yes ({kind})"
        elif not f.get("signed") and kind == "unverified":
            signed = "no (unverified)"
        if not f.get("signed") and f.get("signature_evidence"):
            signed = f"{signed.split(' (')[0]} ({f['signature_evidence']})"
        return [
            ("Account holder", _fmt(f.get("account_holder")), "", "account_holder"),
            ("Account type", _fmt(f.get("account_type")), "", "account_type"),
            ("Bank name", _fmt(f.get("bank_name")), "", "bank_name"),
            ("Bank country", _fmt(f.get("bank_country")), "", "bank_country"),
            ("Bank address", _fmt(f.get("bank_address")), "", "bank_address"),
            ("IBAN", _fmt(iban) + iban_extra, "", "iban"),
            ("SWIFT/BIC", _fmt(f.get("swift_bic")), "", "swift_bic"),
            ("Account number", _fmt(f.get("account_number")), "", "account_number"),
            ("Routing/ABA (standard)", _fmt(f.get("routing_aba")), "", "routing_aba"),
            ("Routing/ABA (wires)", _fmt(f.get("routing_aba_wires")), "", "routing_aba_wires"),
            ("Branch code", _fmt(f.get("branch_code")), "", "branch_code"),
            ("Currency", _fmt(f.get("currency")), "", "currency"),
            ("Document date", _fmt(f.get("doc_date"), empty="no visible document date"), "", "doc_date"),
            ("Signed/stamped", signed, "", "signed"),
        ]
    if _is_w8_pub(pub):
        ftin = f.get("foreign_tin", {}) if isinstance(f.get("foreign_tin"), dict) else {}
        ustin = f.get("us_tin", {}) if isinstance(f.get("us_tin"), dict) else {}
        signed = _fmt(f.get("signed", False))
        if f.get("sign_date"):
            signed += f" ({f['sign_date']})"
        if f.get("signer_name"):
            signed += f" — {f['signer_name']}"
        return [
            ("Form variant", _fmt(f.get("form_variant")), "", "form_variant"),
            ("Beneficial owner (Part I)", _fmt(f.get("legal_name")), "SAP Name 1", "legal_name"),
            ("Country of incorporation", _fmt(f.get("country_incorporation")), "", "country_incorporation"),
            ("Chapter 3 status", _fmt(f.get("chapter3_status")), "", "chapter3_status"),
            ("Chapter 4 (FATCA) status", _fmt(f.get("chapter4_status")), "", "chapter4_status"),
            ("Chapter 4 certification", _fmt(f.get("chapter4_cert_section")), "", "chapter4_cert_section"),
            ("Foreign TIN (masked)", _fmt(ftin), "NEVER → Tax Number 1/2", "foreign_tin"),
            ("US TIN (masked)", _fmt(ustin), "Tax Number 2 only if a real US EIN", "us_tin"),
            ("Address — street", _fmt(f.get("address_street")), "", "address_street"),
            ("Address — city/country", _fmt(f.get("address_city_country")), "", "address_city_country"),
            ("Treaty country", _fmt(f.get("treaty_country")), "", "treaty_country"),
            ("Capacity to sign checked", _fmt(f.get("capacity_checked", False)), "", "capacity_checked"),
            ("Signed (Certification Part)", signed, "", "signed"),
        ]
    tin = f.get("tin", {}) if isinstance(f.get("tin"), dict) else {}
    tin_target = ("Tax Number 1" if tin.get("type") == "SSN"
                  else "Tax Number 2" if tin.get("type") == "EIN" else "")
    signed = _fmt(f.get("signed", False))
    if f.get("signed") and str(f.get("signature_kind") or "") in ("wet", "electronic"):
        signed += f" ({f['signature_kind']})"
    if f.get("sign_date"):
        signed += f" ({f['sign_date']})"
    return [
        ("Line 1 — taxpayer name", _fmt(f.get("line1_name")), "SAP Name 1", "line1_name"),
        ("Line 2 — business name", _fmt(f.get("line2_business_name")), "SAP Name 2", "line2_business_name"),
        ("Classification (Line 3)", _fmt(f.get("line3_classification")), "", "line3_classification"),
        ("TIN type", tin.get("type") or "—", tin_target, "tin"),
        ("TIN (masked)", _fmt(tin), "", "tin"),
        ("Address — street", _fmt(f.get("address_street")), "", "address_street"),
        ("Address — city/state/ZIP", _fmt(f.get("address_city_state_zip")), "", "address_city_state_zip"),
        ("Signed", signed, "", "signed"),
    ]


def data_block(pub: dict) -> str:
    rows = _data_rows(pub)
    w = max(len(r[0]) for r in rows)
    lines = ["─" * 18 + " EXTRACTED DATA " + "─" * 18]
    for label, value, hint, _key in rows:
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
    tpl = _env.get_template("report_bank.md.j2" if pub["doc_class"] == "bank"
                            else "report_w8.md.j2" if _is_w8_pub(pub)
                            else "report_w9.md.j2")
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
        "provenance": pub.get("provenance", {}),
        "crosscheck": pub.get("crosscheck", []),
        "sap_compare": pub.get("sap_compare", []),
        "web_evidence": pub.get("web_evidence", []),
        "warnings": pub.get("warnings", []),
        "sensitive_present": pub.get("sensitive_present", {}),
        "model": pub.get("model"),
        "json_valid_first_try": pub.get("json_valid_first_try"),
        "ts": meta.get("ts"),
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)
