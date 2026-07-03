#!/usr/bin/env python3
"""
stage_b.py — structured extraction (THE trainable stage): raw text -> JSON
{doc_type, fields}, in TWO tiers:

  FAST   — our custom mdmdoc-extract (system prompt + operator exemplars baked)
  STRONG — qwen3:14b, invoked ONLY when the fast result leaves a critical gap
           (US bank letter without routing, W-9 without TIN, crosscheck
           mismatch, invalid JSON, or the operator asked for --quality)

The model classifies and extracts ONLY — verdicts come from the rule engine.
Deterministic regex candidates outrank both tiers on critical IDs; the vision
signature probe outranks both tiers on the `signed` flag.
The prompt contains full sensitive values and is therefore NEVER persisted.
"""
from __future__ import annotations

import json
import re

from . import config, model_client as mc
from .fields import (BANK_DOC_TYPES, BANK_KEYS, W9_DOC_TYPES, W9_KEYS, Extraction,
                     crosscheck_ids, to_iso2)
from .stage_a import RawDoc

_DATE_SHAPE = re.compile(r"^\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$")


def _load_system(doc_class: str) -> str:
    p = config.PROMPTS_DIR / f"system_{'bank' if doc_class == 'bank' else 'w9'}.txt"
    return p.read_text() if p.exists() else ""


def _load_fewshot(doc_class: str) -> list[dict]:
    p = config.FEWSHOT_DIR / f"{'bank' if doc_class == 'bank' else 'w9'}.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def build_prompt(raw: RawDoc, role: str = "TEXT") -> str:
    doc_class = raw.doc_class
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    parts = []
    # our custom model has the exemplars baked in via MESSAGE pairs; any stock
    # model (incl. the strong tier) gets them injected at runtime
    if not mc.resolve(role).startswith("mdmdoc-extract"):
        for ex in _load_fewshot(doc_class):
            parts.append("EXAMPLE INPUT:\n" + ex.get("input", "")
                         + "\nEXAMPLE OUTPUT:\n" + json.dumps(ex.get("output", {}), ensure_ascii=False))
    packet_note = ""
    if raw.bank_letter_pages:
        packet_note = ("\nPACKET SIGNALS: page(s) "
                       + ", ".join(str(p + 1) for p in raw.bank_letter_pages)
                       + " look like a bank-issued confirmation letter"
                       + (("; page(s) " + ", ".join(str(p + 1) for p in raw.invoice_pages)
                           + " look like an invoice") if raw.invoice_pages else "")
                       + ". A packet that CONTAINS a bank confirmation letter is classified "
                         "by that letter (doc_type bank_letter) — invoice pages elsewhere "
                         "do not make the packet an invoice.")
    cand = {k: v for k, v in raw.regex_candidates.items()}
    parts.append(
        "DOCUMENT FILENAME: " + raw.path.rsplit("/", 1)[-1]
        + ("\nHEURISTIC TYPE HINT: " + raw.type_hint if raw.type_hint else "")
        + packet_note
        + "\nOCR-VERIFIED CANDIDATES (from deterministic regex — trust these over your own reading):\n"
        + json.dumps(cand, ensure_ascii=False)
        + "\n\nDOCUMENT TEXT:\n" + raw.raw_text[:config.STAGE_B_TEXT_LIMIT]
        + "\n\nReturn JSON with exactly these keys: doc_type (one of "
        + ", ".join(types) + "), fields {" + ", ".join(keys) + "}. "
        + "Use \"\" for absent string fields, false for absent boolean flags. Do not invent values."
    )
    return "\n\n".join(parts)


def _run_model(raw: RawDoc, role: str) -> tuple[dict | None, bool, str]:
    """One tier run -> (fields-bearing obj or None, json_first_try, model_id)."""
    obj, first_try = mc.generate_json(role, build_prompt(raw, role),
                                      system=_load_system(raw.doc_class),
                                      options={"num_ctx": 16384, "temperature": 0, "seed": 7})
    mc.unload(role)
    return (obj if isinstance(obj, dict) else None), first_try, mc.resolve(role)


def _fields_from(obj: dict, keys: list) -> dict:
    flds = obj.get("fields") if isinstance(obj.get("fields"), dict) else obj
    return {k: flds.get(k, "") for k in keys}


def escalation_reasons(ext: Extraction, raw: RawDoc, quality: bool) -> list[str]:
    """Pure decision function (unit-testable): why the strong tier should run.
    Evaluated AFTER the crosscheck — the regex fill is tier zero."""
    r: list[str] = []
    f = ext.fields
    if quality:
        r.append("quality-requested")
    if not ext.json_valid_first_try:
        r.append("json-retry")
    if ext.doc_class == "bank" and ext.doc_type not in ("invoice", "email", "editable_source"):
        us_ish = (to_iso2(str(f.get("bank_country") or "")) == "US"
                  or "routing_aba" in raw.regex_candidates
                  or bool(re.search(r"(?i)\b(aba|routing)\b", raw.raw_text)))
        if us_ish and not str(f.get("routing_aba") or "").strip():
            r.append("us-bank-no-routing")
        if not str(f.get("account_number") or "").strip() and not str(f.get("iban") or "").strip():
            r.append("bank-no-account-id")
    if ext.doc_class == "w9" and ext.doc_type == "w9":
        if not str(f.get("tin_raw") or "").strip():
            r.append("w9-no-tin")
        if not str(f.get("line3_classification") or "").strip():
            r.append("w9-no-classification")
        if not str(f.get("line1_name") or "").strip():
            r.append("w9-no-line1")
    if any("MISMATCH" in n for n in ext.crosscheck):
        r.append("crosscheck-mismatch")
    return r


def _merge_tiers(fast: dict, strong: dict, keys: list, policy: str = "masked") -> tuple[dict, list]:
    """Field-wise merge. Strong fills gaps and wins disagreements (with a note);
    strong NEVER blanks a non-empty fast value (absence is not evidence)."""
    from .privacy import FIELD_KIND, display_value
    merged, notes = dict(fast), []
    for k in keys:
        fv, sv = fast.get(k, ""), strong.get(k, "")
        if isinstance(fv, bool) or isinstance(sv, bool):
            fvb = fv if isinstance(fv, bool) else str(fv).lower() in ("true", "yes")
            svb = sv if isinstance(sv, bool) else str(sv).lower() in ("true", "yes")
            if fvb != svb:
                notes.append(f"tier disagreement: {k} (fast={fvb}, strong={svb})")
            merged[k] = svb
            continue
        fs, ss = str(fv or "").strip(), str(sv or "").strip()
        if not fs and ss:
            merged[k] = ss
        elif fs and ss and fs.casefold() != ss.casefold():
            kind = FIELD_KIND.get(k)
            shown_f = display_value(kind, fs, policy) if kind else fs
            shown_s = display_value(kind, ss, policy) if kind else ss
            notes.append(f"tier disagreement: {k} (fast={shown_f}, strong={shown_s})")
            merged[k] = ss
    return merged, notes


def _normalize_tin(ext: Extraction) -> None:
    """tin_type to canonical values; a DATE can never be a TIN (the model kept
    grabbing the signature date 1/1/2026 as a tax number)."""
    if ext.doc_class != "w9":
        return
    tt = str(ext.fields.get("tin_type") or "").lower()
    ext.fields["tin_type"] = ("SSN" if "ssn" in tt or "social" in tt
                              else "EIN" if "ein" in tt or "employer" in tt else "")
    tin = str(ext.fields.get("tin_raw") or "").strip()
    if tin and ("/" in tin or _DATE_SHAPE.match(tin)):
        ext.warnings.append(f"model put a date-shaped value into tin_raw — discarded")
        if not str(ext.fields.get("sign_date") or "").strip() and "/" in tin:
            ext.fields["sign_date"] = tin
            ext.provenance["sign_date"] = {"source": "rule", "page": None}
        ext.fields["tin_raw"] = ""
        ext.provenance.pop("tin_raw", None)


def _exemplar_values(doc_class: str) -> set:
    """All string values that appear in few-shot exemplar OUTPUTS. These are
    shape-preserving fakes / example data — a real document can never
    legitimately contain them; if the model outputs one, it echoed the exemplar."""
    vals: set = set()
    for ex in _load_fewshot(doc_class):
        out = ex.get("output", {})
        for v in (out.get("fields") or {}).values():
            s = str(v or "").strip()
            if len(s) >= 4 and not isinstance(v, bool):
                vals.add(s.casefold())
    return vals


def _drop_exemplar_echo(ext: Extraction, raw: RawDoc) -> None:
    """Real case: a W-8 came back with 'ACME' and an exemplar's fake EIN —
    the model copied the few-shot example instead of reading the document.
    Any extracted value that equals an exemplar value AND does not occur in
    the document text is an echo — drop it."""
    exemplar_vals = _exemplar_values(ext.doc_class)
    if not exemplar_vals:
        return
    doc_text = raw.raw_text.casefold()
    for k, v in list(ext.fields.items()):
        if isinstance(v, bool):
            continue
        s = str(v or "").strip()
        if s and s.casefold() in exemplar_vals and s.casefold() not in doc_text:
            ext.fields[k] = ""
            ext.provenance.pop(k, None)
            ext.warnings.append(f"{k}: dropped few-shot exemplar echo (value was "
                                "copied from an example, not read from the document)")


_NAME_FIELDS = ("line1_name", "line2_business_name", "account_holder", "bank_name")


def _drop_filename_echo(ext: Extraction, raw: RawDoc) -> None:
    """Real case: line1 came back as 'Dr. Clarke' — lifted from the FILENAME
    ('...Donation from Dr. Clarke.pdf'), not from the form. A name value that
    occurs in the filename but nowhere in the document text is not a reading."""
    fname = raw.path.rsplit("/", 1)[-1].casefold()
    doc_text = raw.raw_text.casefold()
    for k in _NAME_FIELDS:
        s = str(ext.fields.get(k) or "").strip()
        if len(s) >= 4 and s.casefold() in fname and s.casefold() not in doc_text:
            ext.fields[k] = ""
            ext.provenance.pop(k, None)
            ext.warnings.append(f"{k}: dropped filename echo ('{s}' appears in the "
                                "file name but not in the document)")


def _apply_w9_zone_probe(ext: Extraction, raw: RawDoc) -> None:
    """Zone-crop vision evidence SETTLES the checkbox classification and the TIN:
    a checked box and box digits are visual facts — a text-transcription guess
    must never override them (real case: 'Individual' guessed while S corporation
    was checked; boxed EIN skipped entirely)."""
    probe = raw.w9_probe
    if not probe or ext.doc_class != "w9" or ext.doc_type != "w9":
        return  # zone coordinates are W-9-specific; never apply to a W-8
    probe_page = probe.get("page", 0) + 1 if isinstance(probe.get("page"), int) else None
    vis_class = str(probe.get("classification") or "").strip()
    if vis_class:
        cur = str(ext.fields.get("line3_classification") or "").strip()
        shown = vis_class + (f" ({probe['llc_code']})" if probe.get("llc_code")
                             and vis_class.lower().startswith("llc") else "")
        if cur and cur.casefold() != vis_class.casefold():
            ext.warnings.append(f"classification: visual checkbox = {vis_class}, "
                                f"text model said {cur} — using the visual evidence")
        ext.fields["line3_classification"] = shown
        ext.provenance["line3_classification"] = {"source": "zone-probe", "page": probe_page}
    digits = str(probe.get("tin_digits") or "")
    ttype = str(probe.get("tin_type") or "")
    if len(digits) == 9 and ttype in ("SSN", "EIN"):
        formatted = (f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if ttype == "SSN"
                     else f"{digits[:2]}-{digits[2:]}")
        cur = str(ext.fields.get("tin_raw") or "").strip()
        from .fields import _norm_id
        if cur and _norm_id(cur) != digits:
            from .privacy import mask
            ext.warnings.append(f"tin: TIN-box crop reads {mask(ttype.lower(), formatted)}, "
                                f"text model said {mask('tin', cur)} — using the box digits")
        elif not cur:
            ext.crosscheck.append(f"tin=filled-from-TIN-box-crop"
                                  f"({'EIN' if ttype == 'EIN' else 'SSN'})")
        ext.fields["tin_raw"] = formatted
        ext.fields["tin_type"] = ttype
        ext.provenance["tin_raw"] = {"source": "zone-probe", "page": probe_page}


def _apply_signature_probe(ext: Extraction, raw: RawDoc) -> None:
    """The vision verdict on the signature outranks both text tiers: signatures
    are pixels, not text. Stamp counts for bank letters, not for W-9."""
    probe = raw.signature_probe
    if not probe:
        return
    from .privacy import scrub_text
    probe_page = probe.get("page", 0) + 1 if isinstance(probe.get("page"), int) else None
    visual = bool(probe.get("handwritten_signature")) or (
        ext.doc_class == "bank" and bool(probe.get("stamp")))
    model_said = ext.fields.get("signed")
    model_said = model_said if isinstance(model_said, bool) else \
        str(model_said).lower() in ("true", "yes")
    if visual != model_said:
        ext.warnings.append(f"signature: vision says {visual}, text model said {model_said}")
    ext.fields["signed"] = visual
    ext.provenance["signed"] = {"source": "vision-crop", "page": probe_page}
    # the probe often reads the handwritten date next to the signature
    sig_date = str(probe.get("date_near_signature") or "").strip()
    if sig_date and ext.doc_class == "w9" and not str(ext.fields.get("sign_date") or "").strip():
        ext.fields["sign_date"] = sig_date
        ext.provenance["sign_date"] = {"source": "vision-crop", "page": probe_page}
    evidence = scrub_text(str(probe.get("evidence") or ""), ext.vault)
    ext.signature_probe = {"handwritten_signature": bool(probe.get("handwritten_signature")),
                           "stamp": bool(probe.get("stamp")), "evidence": evidence,
                           "page": probe_page}
    if ext.doc_class == "bank" and not visual and evidence:
        ext.fields["signature_evidence"] = evidence
        ext.provenance["signature_evidence"] = {"source": "vision-crop", "page": probe_page}


def _attribute_page(raw: RawDoc, value) -> int | None:
    """Which page (1-based) a value was read from — by searching the per-page
    texts kept in memory. Best-effort: None when the pages can't be told apart."""
    from .fields import _norm_id
    if isinstance(value, bool):
        return None
    s = str(value or "").strip()
    if not s:
        return None
    if len(raw.pages_used) == 1:
        return raw.pages_used[0] + 1
    sc = s.casefold()
    for i in raw.pages_used:
        if sc in (raw.page_texts.get(i) or "").casefold():
            return i + 1
    s_id = _norm_id(s)
    if len(s_id) >= 4 and s_id.isdigit():
        for i in raw.pages_used:
            if s_id in _norm_id(raw.page_texts.get(i) or ""):
                return i + 1
    return None


def _finalize_provenance(ext: Extraction, raw: RawDoc) -> None:
    """Every non-empty field gets a provenance entry: special sources (probes,
    OCR fills, guards) were recorded where they fired; everything else was read
    by the text model. Pages are attributed by per-page text search."""
    for k, v in ext.fields.items():
        filled = isinstance(v, bool) or str(v or "").strip()
        if not filled:
            ext.provenance.pop(k, None)
            continue
        p = ext.provenance.setdefault(k, {"source": "model", "page": None})
        if p.get("page") is None and not isinstance(v, bool):
            p["page"] = _attribute_page(raw, v)
    ext.provenance.setdefault("doc_type", {"source": "model", "page": None})


def extract(raw: RawDoc, quality: bool = False, policy: str = "masked") -> Extraction:
    doc_class = raw.doc_class
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    ext_res = Extraction(doc_class=doc_class, model_id=mc.resolve("TEXT"))
    ext_res.warnings = list(raw.warnings)

    # deterministic overrides that need no model
    if raw.editable:
        ext_res.doc_type = "editable_source"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}
    elif raw.ext in config.EMAIL_EXTS:
        ext_res.doc_type = "email"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}

    if raw.raw_text.strip():
        obj, first_try, model_id = _run_model(raw, "TEXT")
        ext_res.model_id = model_id
        ext_res.json_valid_first_try = first_try and obj is not None
        if obj is not None:
            model_type = str(obj.get("doc_type", "") or "").strip().lower()
            if not ext_res.doc_type:  # deterministic overrides win
                ext_res.doc_type = model_type if model_type in types else (raw.type_hint or "other")
            ext_res.fields = _fields_from(obj, keys)
        else:
            ext_res.warnings.append("stage-B model returned no valid JSON")
            ext_res.doc_type = ext_res.doc_type or raw.type_hint or ("other" if doc_class == "bank" else "unknown")
            ext_res.fields = {k: "" for k in keys}
    else:
        ext_res.doc_type = ext_res.doc_type or raw.type_hint or ("other" if doc_class == "bank" else "unknown")
        ext_res.fields = {k: "" for k in keys}
        if not raw.locked and not raw.editable:
            ext_res.warnings.append("no text available for extraction")

    # packet-aware classification: a bank confirmation letter inside the packet
    # beats invoice pages elsewhere (the letter IS the banking support)
    if doc_class == "bank" and raw.bank_letter_pages and ext_res.doc_type == "invoice":
        pages = ", ".join(str(p + 1) for p in raw.bank_letter_pages)
        ext_res.warnings.append(
            f"packet contains invoice page(s), but classified by the bank confirmation "
            f"letter on page {pages} — an invoice elsewhere does not poison the packet")
        ext_res.doc_type = "bank_letter"
        ext_res.provenance["doc_type"] = {"source": "rule",
                                          "page": raw.bank_letter_pages[0] + 1}
    # deterministic type hints beat a hesitant model on hard-reject types
    elif raw.type_hint == "invoice" and ext_res.doc_type not in ("invoice",):
        ext_res.warnings.append(f"type hint 'invoice' overrides model '{ext_res.doc_type}'")
        ext_res.doc_type = "invoice"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}

    # echo guards run BEFORE escalation: a dropped echo leaves a gap the strong
    # tier must be given the chance to fill
    _drop_exemplar_echo(ext_res, raw)
    _drop_filename_echo(ext_res, raw)
    _normalize_tin(ext_res)
    ext_res.crosscheck = crosscheck_ids(ext_res.fields, raw.regex_candidates,
                                        doc_class, policy=policy,
                                        prov=ext_res.provenance)

    # --- escalation to the strong tier (quality first) -------------------------
    reasons = escalation_reasons(ext_res, raw, quality)
    if reasons and raw.raw_text.strip() and mc.strong_distinct():
        strong_obj, strong_ok, strong_model = _run_model(raw, "TEXT_STRONG")
        ext_res.strong_json_valid = strong_ok and strong_obj is not None
        if strong_obj is not None:
            strong_fields = _fields_from(strong_obj, keys)
            merged, tier_notes = _merge_tiers(ext_res.fields, strong_fields, keys, policy)
            ext_res.fields = merged
            ext_res.warnings += tier_notes
            # the strong tier reads the same filename-bearing prompt — re-guard
            _drop_exemplar_echo(ext_res, raw)
            _drop_filename_echo(ext_res, raw)
            strong_type = str(strong_obj.get("doc_type", "") or "").strip().lower()
            if (strong_type in types and not raw.editable
                    and raw.ext not in config.EMAIL_EXTS
                    and not (doc_class == "bank" and raw.bank_letter_pages
                             and strong_type == "invoice")
                    and raw.type_hint != "invoice"):
                ext_res.doc_type = strong_type
            _normalize_tin(ext_res)
            # regex stays the highest authority — re-run the crosscheck on the merge
            ext_res.crosscheck = crosscheck_ids(ext_res.fields, raw.regex_candidates,
                                                doc_class, policy=policy,
                                                prov=ext_res.provenance)
            ext_res.tier, ext_res.model_strong = "strong", strong_model
        else:
            ext_res.warnings.append("strong tier returned no valid JSON — fast result kept")
    ext_res.escalated_because = reasons

    _apply_w9_zone_probe(ext_res, raw)
    _normalize_tin(ext_res)          # zone TIN passes through the date guard too
    _apply_signature_probe(ext_res, raw)
    _finalize_provenance(ext_res, raw)

    ext_res.register_secrets()
    # regex candidates hold full values too — register so the leak gate knows them
    from .privacy import FIELD_KIND
    for k, v in raw.regex_candidates.items():
        if k in ("iban", "account_number", "routing_aba", "routing_aba_wires",
                 "ein", "tin_boxed"):
            ext_res.vault.register(FIELD_KIND.get(k, "account_number"), v)
    return ext_res
