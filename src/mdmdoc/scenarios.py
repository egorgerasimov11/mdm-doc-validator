#!/usr/bin/env python3
"""
scenarios.py — the scenario taxonomy behind the closed learning loop.

A scenario tag names a FAILURE-SHAPED situation (w9_boxed_tin, bank_invoice_
plus_letter, ...) so that labels, eval slices, few-shot coverage and the
training queue all speak the same language. Tags are stored on labels,
suggested automatically from run artifacts, and free-form tags are allowed
(normalized to snake_case) — the canonical list below is the vocabulary the
UI offers, not a hard schema.
"""
from __future__ import annotations

import re

SCENARIOS = {
    "bank": [
        "bank_invoice_plus_letter",   # welcome packet: invoice template + real bank letter
        "bank_invoice_only",          # pure invoice — must stay REJECT
        "bank_typed_officer_block",   # typed officer signature block (BNK-026)
        "bank_image_only",            # scan/photo, no text layer
        "bank_rotated_photo",         # OSD rotation applied
        "bank_multi_aba",             # separate ACH vs wire routing numbers
        "bank_sap_compare",           # run included an SAP screenshot comparison
        "bank_supplier_letterhead",   # payment instructions on supplier letterhead
        "bank_email_support",         # support evidence is an email
    ],
    "w9": [
        "w9_image_only",              # scanned/photographed W-9
        "w9_checkbox_error",          # classification checkbox misread (zone probe case)
        "w9_boxed_tin",               # TIN in per-digit boxes
        "w9_line_swap",               # Line 1 / Line 2 swapped or collapsed (W9-013)
        "w9_unsigned",                # no signature
        "w9_rotated_photo",
        "w9_handwritten",             # handwritten entries
        "w8_form",                    # W-8 instead of W-9
    ],
}

# Where the mistake was made — decides WHAT to fix (Stage A / Stage B / rules).
ERROR_SOURCES = [
    "ocr_missed",           # perception: text/zone never reached the model -> Stage A
    "model_mapped_wrong",   # text was there, mapping wrong -> Stage B / few-shot
    "rule_wrong",           # fields right, verdict wrong -> rules/*.yaml
    "doc_type_wrong",       # misclassified document
    "workbook_mismatch",    # document fine, workbook disagrees
]

_TAG_RE = re.compile(r"[^a-z0-9]+")


def normalize_tags(tags) -> list[str]:
    """Free-form input -> deduped snake_case tags, canonical order first."""
    out: list[str] = []
    for t in tags or []:
        t = _TAG_RE.sub("_", str(t or "").strip().lower()).strip("_")
        if t and t not in out:
            out.append(t)
    return out


def options_for(doc_class: str) -> list[str]:
    return list(SCENARIOS.get(doc_class, []))


def suggest(meta: dict, stage_a_pub: dict, pub: dict, findings: list) -> list[str]:
    """Infer likely scenario tags from run artifacts (all masked/public views).
    Suggestions pre-fill the review form — the operator confirms or edits."""
    doc_class = (meta or {}).get("doc_class", "bank")
    sa = stage_a_pub or {}
    pub = pub or {}
    warnings = [str(w) for w in (pub.get("warnings") or [])]
    rule_ids = {str((f or {}).get("rule_id", "")) for f in (findings or [])}
    cand = sa.get("regex_candidates_masked") or {}
    fields = pub.get("fields") or {}
    tags: list[str] = []

    if sa.get("has_text_layer") is False:
        tags.append(f"{doc_class}_image_only")
    if sa.get("rotations"):
        tags.append(f"{doc_class}_rotated_photo")

    if doc_class == "bank":
        if sa.get("bank_letter_pages") and sa.get("invoice_pages"):
            tags.append("bank_invoice_plus_letter")
        elif pub.get("doc_type") == "invoice":
            tags.append("bank_invoice_only")
        if "BNK-026" in rule_ids:
            tags.append("bank_typed_officer_block")
        if cand.get("routing_aba") and cand.get("routing_aba_wires"):
            tags.append("bank_multi_aba")
        if (meta or {}).get("sap_path"):
            tags.append("bank_sap_compare")
        if pub.get("doc_type") == "supplier_letterhead":
            tags.append("bank_supplier_letterhead")
        if pub.get("doc_type") == "email":
            tags.append("bank_email_support")
    else:
        if cand.get("tin_boxed"):
            tags.append("w9_boxed_tin")
        if any("visual checkbox" in w for w in warnings):
            tags.append("w9_checkbox_error")
        if "W9-013" in rule_ids:
            tags.append("w9_line_swap")
        if fields.get("signed") is False:
            tags.append("w9_unsigned")
        if pub.get("doc_type") == "w8":
            tags.append("w8_form")

    return normalize_tags(tags)
