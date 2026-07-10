#!/usr/bin/env python3
"""
confidence.py — a deterministic read-confidence assessment folded from signals
the pipeline ALREADY records (no new extraction, no model call).

Purpose: abstain to manual review instead of blindly ACCEPTing when the read is
shaky. The verdict still comes only from human-approved rules (invariant) — this
layer can ONLY escalate an ACCEPT to NEED_MANUAL_REVIEW via the CONF-001 finding
in pipeline.run_check, never relax a verdict.

level:
  * high   — nothing weak.
  * medium — exactly one weak signal.
  * low    — a hard signal (an ID field the OCR regex and the model DISAGREE on,
             or the two engines disagree in dual mode), OR >=2 weak signals.
Weak signals: an unconfirmed model-only critical ID; an escalation reason whose
field is still empty after the strong tier; thin perception (no text layer AND
no vision text, or nothing recovered); an uncertain signature read.
"""
from __future__ import annotations

from .fields import Extraction

# critical identity fields per class — a wrong one here is the dangerous error
_CRIT_IDS = {
    "bank": ("iban", "account_number", "routing_aba", "routing_aba_wires", "swift_bic"),
    "w9": ("tin_raw",),
}


def assess(ext: Extraction, raw=None) -> dict:
    """-> {'level': 'high'|'medium'|'low', 'reasons': [str, ...]}."""
    reasons: list[str] = []
    hard = False
    crit = _CRIT_IDS.get(ext.doc_class, ())

    # HARD: OCR regex and the model disagree on a critical ID (crosscheck note
    # "field=MISMATCH(...)"). Rules read only fields, so an ACCEPT can hide this.
    for note in ext.crosscheck or []:
        n = str(note)
        if "MISMATCH" in n and any(n.startswith(f"{k}=") for k in crit):
            hard = True
            reasons.append(f"OCR/model disagree on {n.split('=', 1)[0]}")

    # HARD: the two analysis engines disagree on a field (dual mode)
    for row in getattr(ext, "engine_compare", None) or []:
        if row.get("agree") is False:
            hard = True
            reasons.append(f"deterministic and LLM engines disagree on {row.get('field')}")

    weak = 0

    # WEAK: a critical ID that only the model produced, never confirmed by OCR
    for k in crit:
        if str(ext.fields.get(k) or "").strip():
            # provenance is keyed by the INTERNAL field name ("tin_raw" included);
            # the "tin" re-key exists only in the public to_public() view.
            prov = (ext.provenance or {}).get(k) or {}
            # "confirmed" = model==regex over the SAME text; confirmed_independent
            # = the ID printed on >=2 pages of different classes (P5) — either
            # settles the model-only worry
            if (prov.get("source") == "model" and not prov.get("confirmed")
                    and not prov.get("confirmed_independent")):
                weak += 1
                reasons.append(f"{k}: model-only read, not confirmed by OCR")

    # WEAK: an escalation was raised for a field that is STILL empty
    for reason in ext.escalated_because or []:
        field = _reason_field(reason)
        if field and not str(ext.fields.get(field) or "").strip():
            weak += 1
            reasons.append(f"unresolved after strong tier: {reason}")

    # WEAK: thin perception — nothing but an empty/near-empty read
    if raw is not None:
        thin = (not getattr(raw, "has_text_layer", False)
                and not (getattr(raw, "vision_text", "") or "").strip())
        if thin or any("no text recovered" in w for w in (getattr(raw, "warnings", []) or [])):
            weak += 1
            reasons.append("thin perception (no text layer, no vision text)")

    # HARD: the vision ensemble stayed CONTESTED after escalation — positive
    # and negative reads of the same signature area (S2); an ACCEPT resting on
    # a contested signature is exactly the dangerous direction
    sig = getattr(ext, "signature_probe", None) or {}
    if sig.get("contested"):
        hard = True
        reasons.append("signature vision contested (positive and negative reads)")
    # WEAK: signature read was flagged uncertain
    elif sig.get("uncertain"):
        weak += 1
        reasons.append("signature read uncertain")

    # WEAK: doc_type was rescued from an ungrounded model guess by deterministic
    # evidence (П3) — right type, shaky provenance; hold a borderline ACCEPT
    if getattr(ext, "doc_type_uncertain", False):
        weak += 1
        reasons.append("doc_type rescued by evidence (model said payment_instructions)")

    if hard or weak >= 2:
        level = "low"
    elif weak == 1:
        level = "medium"
    else:
        level = "high"
    return {"level": level, "reasons": reasons}


def _reason_field(reason: str) -> str:
    """Map an escalation-reason name to the field it is about (best effort)."""
    r = str(reason)
    if "holder" in r:
        return "account_holder"
    if "bank-name" in r:
        return "bank_name"
    if "routing" in r:
        return "routing_aba"
    if "account" in r:
        return "account_number"
    if "no-tin" in r:
        return "tin_raw"
    if "line1" in r:
        return "line1_name"
    if "classification" in r:
        return "line3_classification"
    return ""
