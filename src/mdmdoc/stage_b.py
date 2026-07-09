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
                     crosscheck_ids, iban_mod97_ok, to_iso2)
from .fields import find_valid_ibans as fields_valid_ibans
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


# Strong-tier FOCUS suffixes keyed on escalation reasons: the retry must know
# WHAT the fast pass missed instead of re-reading with the identical prompt.
# Strong-tier only (build_prompt(..., focus=...)) — the fast prompt stays
# byte-identical, so the trainable exemplar format never changes.
FOCUS_HINTS = {
    "bank-no-holder": (
        "PRIOR-PASS GAP: no account holder found. Search again for the "
        "beneficiary / account OWNER: 'account holder', 'beneficiary', "
        "'account name', 'in the name of', 'a nombre de', 'titular', "
        "'Kontoinhaber', 'intestato a', '口座名義', the addressee/company block "
        "above the bank details, the letterhead company name. The holder is "
        "usually a company, often with a legal suffix (GmbH, S.A.S., Ltd)."),
    "bank-no-bank-name": (
        "PRIOR-PASS GAP: no bank name found. Look at the letterhead, logo "
        "caption, footer, stamp and the 'Bank:'/'开户银行' label."),
    "us-bank-no-routing": (
        "PRIOR-PASS GAP: US account without a routing number. Look for a "
        "9-digit ABA / routing number near 'ABA', 'routing', 'ACH' or 'wire' "
        "labels — there may be separate ACH and wire routing numbers."),
    "w9-no-line1": (
        "PRIOR-PASS GAP: Line 1 (legal name) is empty. Line 1 is the FIRST "
        "name box at the top of the W-9 — do not confuse it with Line 2 "
        "(business/DBA name); both may be filled with different names."),
}


def build_prompt(raw: RawDoc, role: str = "TEXT", focus: list[str] | None = None) -> str:
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
    elif raw.invoice_pages:
        packet_note = ("\nPACKET SIGNALS: page(s) "
                       + ", ".join(str(p + 1) for p in raw.invoice_pages)
                       + " carry their OWN invoice number/date/amount-due — that makes "
                         "the packet an invoice (doc_type invoice; never acceptable as "
                         "bank proof), even if other pages are payment instructions. "
                         "Only a page that merely MENTIONS someone else's invoices being "
                         "paid (remittance) without its own invoice number/date/amount is "
                         "doc_type payment_instructions.")
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
        + ("\nClassify by what the document IS, not by keywords: supplier ACH/remittance "
           "payment instructions are doc_type payment_instructions even though they "
           "mention invoices; a real invoice carries its own invoice number, date and "
           "amount due." if doc_class == "bank" else "")
    )
    hints = [FOCUS_HINTS[r] for r in (focus or []) if r in FOCUS_HINTS]
    if hints:
        parts.append("FOCUS:\n" + "\n".join(hints))
    return "\n\n".join(parts)


def _run_model(raw: RawDoc, role: str,
               focus: list[str] | None = None) -> tuple[dict | None, bool, str]:
    """One tier run -> (fields-bearing obj or None, json_first_try, model_id)."""
    obj, first_try = mc.generate_json(role, build_prompt(raw, role, focus=focus),
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
        if not str(f.get("account_holder") or "").strip():
            r.append("bank-no-holder")
        if not str(f.get("bank_name") or "").strip():
            r.append("bank-no-bank-name")
        if ext.doc_type in ("", "other"):
            r.append("bank-type-unclear")
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


def _exemplar_values(doc_class: str, exclude_sha: str = "") -> set:
    """All string values that appear in few-shot exemplar OUTPUTS. These are
    shape-preserving fakes / example data — a real document can never
    legitimately contain them; if the model outputs one, it echoed the exemplar.
    Exemplars built from `exclude_sha` (the document being processed) are
    skipped: its own gold values are NOT echoes."""
    vals: set = set()
    for ex in _load_fewshot(doc_class):
        if exclude_sha and ex.get("doc_sha256") == exclude_sha:
            continue
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
    exemplar_vals = _exemplar_values(ext.doc_class, exclude_sha=raw.sha256)
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


def _cross_note(ext: Extraction, msg: str) -> None:
    if msg not in ext.crosscheck:
        ext.crosscheck.append(msg)


def _audit_bank_ids(ext: Extraction, raw: RawDoc) -> None:
    """Deterministic audit of bank identifiers (idempotent — runs again after the
    strong-tier merge):
    - routing fields hold 9-digit US ABA numbers ONLY; Italian ABI/CAB and other
      domestic codes move into a crosscheck note, verified against the IBAN
      structure when possible (CAB doubles as branch_code);
    - the printed account number wins over the zero-padded IBAN account part;
    - the IBAN mod-97 checksum is stated out loud (audit fact, not silence)."""
    if ext.doc_class != "bank":
        return
    f = ext.fields
    # A value in the IBAN field that is NOT IBAN-shaped (every real IBAN starts
    # with a 2-letter country code) is a plain account number. The US and other
    # non-IBAN countries print domestic/international account numbers, sometimes
    # under an "IBAN account no." label — that is a NORMAL format, not a
    # malformed IBAN, and must never fire BNK-011.
    iban_field = re.sub(r"\s", "", str(f.get("iban") or "")).upper()
    if iban_field and not re.match(r"^[A-Z]{2}\d{2}", iban_field):
        digits = re.sub(r"\D", "", iban_field)
        acct = re.sub(r"\D", "", str(f.get("account_number") or ""))
        if digits and not acct:
            f["account_number"] = digits
            ext.provenance["account_number"] = {"source": "rule", "page": None}
            _cross_note(ext, "the IBAN-field value is a plain account number "
                             "(this country has no IBAN) — moved to account number")
        elif digits and digits == acct:
            _cross_note(ext, "the IBAN field duplicated the account number "
                             "(no IBAN in this country) — cleared")
        else:
            _cross_note(ext, "the IBAN-field value is a plain account number "
                             "(this country has no IBAN), not a malformed IBAN")
        f["iban"] = ""
        ext.provenance.pop("iban", None)
    iban = re.sub(r"\s", "", str(f.get("iban") or "")).upper()
    if iban and not iban_mod97_ok(iban):
        # the model dropped/garbled a character — the DOCUMENT is the authority:
        # look for a checksum-valid IBAN in the raw text (spaced prints included)
        cands = fields_valid_ibans(raw.raw_text)
        same_cc = [c for c in cands if c[:2] == iban[:2]]
        same_tail = [c for c in same_cc if c[-4:] == iban[-4:]]
        pick = (same_tail or same_cc)
        if len(set(pick)) == 1 and pick[0] != iban:
            f["iban"] = pick[0]
            ext.provenance["iban"] = {"source": "ocr-regex", "page": None}
            _cross_note(ext, "iban repaired from the document text (model read "
                             "failed the mod-97 checksum)")
            iban = pick[0]
    if iban:
        _cross_note(ext, "iban checksum (ISO 13616 mod-97): "
                    + ("valid" if iban_mod97_ok(iban) else "INVALID"))
    domestic: list[str] = []
    for k in ("routing_aba", "routing_aba_wires"):
        d = re.sub(r"\D", "", str(f.get(k) or ""))
        if d and len(d) != 9:
            domestic.append(d)
            f[k] = ""
            ext.provenance.pop(k, None)
    if domestic:
        if iban.startswith("IT") and len(iban) >= 15:
            abi, cab = iban[5:10], iban[10:15]
            ok = set(domestic) <= {abi, cab}
            _cross_note(ext, f"domestic bank codes {'/'.join(domestic)} (ABI/CAB, not ABA): "
                             + ("match the IBAN structure" if ok
                                else f"do NOT match the IBAN structure (ABI {abi} / CAB {cab})"))
            if not str(f.get("branch_code") or "").strip():
                f["branch_code"] = cab
                ext.provenance["branch_code"] = {"source": "rule", "page": None}
        else:
            _cross_note(ext, "domestic bank code(s) (not a US ABA), removed from "
                             f"routing fields: {'/'.join(domestic)}")
    acct = re.sub(r"\D", "", str(f.get("account_number") or ""))
    if iban and acct and acct.lstrip("0") != acct and iban.endswith(acct) \
            and acct.lstrip("0") in (raw.raw_text or ""):
        printed = acct.lstrip("0")
        f["account_number"] = printed
        _cross_note(ext, f"account number: printed form {printed} "
                         f"(IBAN account part is the zero-padded {acct})")


def _fix_jp_form(ext: Extraction, raw: RawDoc) -> None:
    """Japanese domestic bank forms: 〒NNN-NNNN is a POSTAL CODE, not an account;
    labeled 口座番号/支店 fields rescue the real values; country is inferable.
    NFKC first — JP documents print digits/hyphens full-width (８１３−００４４),
    which no ASCII regex would ever match."""
    if ext.doc_class != "bank":
        return
    import unicodedata
    text = unicodedata.normalize("NFKC", raw.raw_text or "")
    if "〒" not in text and "口座" not in text:
        return
    f = ext.fields
    acct = re.sub(r"\D", "", str(f.get("account_number") or ""))
    if len(acct) == 7 and re.search(rf"〒\s*{acct[:3]}[-‐−–]?\s*{acct[3:]}", text):
        ext.warnings.append(f"account_number …{acct[-4:]} equalled the postal code (〒) — dropped")
        f["account_number"] = ""
        ext.provenance.pop("account_number", None)
    if not str(f.get("account_number") or "").strip():
        m = re.search(r"(?:口座番号|Account\s*Number)[^\d]{0,20}(\d{4,10})", text)
        if m:
            f["account_number"] = m.group(1)
            ext.provenance["account_number"] = {"source": "ocr-regex", "page": None}
            _cross_note(ext, "account number taken from the labeled 口座番号/Account Number field")
    # split-stream forms (labels and values live in separate text runs): the
    # account value comes BEFORE the bank-name token in the value stream, while
    # a postal-code-like token from the address block comes after it
    acct = re.sub(r"\D", "", str(f.get("account_number") or ""))
    bank_pos = text.find("銀行")
    if acct and bank_pos > 0 and text.find(acct) > bank_pos:
        earlier = {m.group(0) for m in re.finditer(r"\b\d{6,8}\b", text[:bank_pos])}
        earlier.discard(acct)
        if len(earlier) == 1:
            new = earlier.pop()
            ext.warnings.append(
                f"account_number …{acct[-4:]} sits after the bank name (address zone) — "
                f"took …{new[-4:]} from the form-value stream instead; verify")
            f["account_number"] = new
            ext.provenance["account_number"] = {"source": "rule", "page": None}
    if not str(f.get("branch_code") or "").strip():
        m = re.search(r"(?:支店(?:番号|コード|名)?|Branch\s*(?:Name/number|Number|No\.?|Code)?)"
                      r"\s*[:：]?\s*0?(\d{3})\b", text)
        if m:
            f["branch_code"] = m.group(1)
            ext.provenance["branch_code"] = {"source": "ocr-regex", "page": None}
    if "普通" in text and not str(f.get("account_type") or "").strip():
        f["account_type"] = "普通口座 (ordinary account)"
        ext.provenance["account_type"] = {"source": "ocr-regex", "page": None}
    if not str(f.get("bank_country") or "").strip():
        f["bank_country"] = "JP"
        ext.provenance["bank_country"] = {"source": "rule", "page": None}
        _cross_note(ext, "bank country inferred: JP (Japanese domestic form markers)")


def _fix_statement_period(ext: Extraction, raw: RawDoc) -> None:
    """A statement's date IS its period — rescue it when the model left doc_date
    empty ('no visible document date' on a dated statement reads as a miss)."""
    if ext.doc_class != "bank" or ext.doc_type != "bank_statement":
        return
    if str(ext.fields.get("doc_date") or "").strip():
        return
    m = re.search(r"(\d{1,2}[- ]?[A-Za-z]{3}[- ]?\d{4})\s*(?:to|through|–|—|-)\s*"
                  r"(\d{1,2}[- ]?[A-Za-z]{3}[- ]?\d{4})", raw.raw_text or "")
    if m:
        ext.fields["doc_date"] = f"{m.group(1)} to {m.group(2)}"
        ext.provenance["doc_date"] = {"source": "ocr-regex", "page": None}
        _cross_note(ext, "document date = statement period")


def _esignature_guard(ext: Extraction, raw: RawDoc) -> None:
    """A DocuSign/Adobe-Sign envelope IS a signature (electronic) — 'unsigned'
    would be wrong; the timestamp near it is the signing date."""
    text_low = (raw.raw_text or "").lower()
    if ext.doc_class != "bank" or ext.fields.get("signed"):
        return
    if "docusign envelope" in text_low or "docusigned by" in text_low \
            or "adobe sign" in text_low:
        ext.fields["signed"] = True
        ext.fields["signature_evidence"] = \
            "electronically signed (DocuSign/e-signature envelope present)"
        ext.provenance["signed"] = {"source": "rule", "page": None}
        if not str(ext.fields.get("doc_date") or "").strip():
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s*\|\s*\d{1,2}:\d{2}", raw.raw_text or "")
            if m:
                ext.fields["doc_date"] = m.group(1)
                ext.provenance["doc_date"] = {"source": "ocr-regex", "page": None}


def _ground_payment_instructions(ext: Extraction, raw: RawDoc) -> None:
    """A payment_instructions classification must be GROUNDED: the document has
    to carry at least one deterministic payment-instruction marker (see
    fields._PAYMENT_INSTRUCTION_MARKS). An ungrounded model guess falls back to
    the deterministic type hint (or 'other'). Real case: a Chinese conference
    exhibition notice (招商通知) with the organiser's remittance details was
    guessed as payment_instructions — it is not the vendor's banking document."""
    if ext.doc_class != "bank" or ext.doc_type != "payment_instructions":
        return
    from .fields import payment_instruction_marks
    if payment_instruction_marks(raw.raw_text):
        return
    new_type = raw.type_hint or "other"
    if new_type == "payment_instructions":
        return
    ext.warnings.append(
        "model classified payment_instructions but the document carries no "
        f"payment-instruction markers — classified as {new_type}")
    ext.doc_type = new_type
    ext.provenance["doc_type"] = {"source": "rule", "page": None}


_BANKNAME_NOISE = ("vigilado", "superintendencia", "supervised by", "regulated by")


def _drop_regulator_noise(ext: Extraction) -> None:
    """Regulator watermarks (e.g. Colombia's margin stamp 'VIGILADO
    Superintendencia Financiera') get read as the bank name — drop them so the
    gap escalates to the strong tier instead of persisting as a wrong value."""
    v = str(ext.fields.get("bank_name") or "")
    if v and any(n in v.lower() for n in _BANKNAME_NOISE):
        ext.warnings.append(f"bank_name '{v}' looks like a regulator watermark — dropped")
        ext.fields["bank_name"] = ""
        ext.provenance.pop("bank_name", None)


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
    # name boxes (positional read of Line 1 / Line 2). Rules, in order:
    # fill-empty; swap-repair (text line1 == zone line2 while zone line1 differs);
    # any other disagreement keeps the TEXT value (names are not checkboxes) with
    # a warning; a zone value NEVER blanks a non-empty field; printed-caption
    # echoes are dropped.
    _CAPTIONS = ("as shown on", "income tax return", "business name",
                 "disregarded entity", "name of entity")

    def _zone_name(key: str) -> str:
        v = str(probe.get(key) or "").strip()
        return "" if any(c in v.lower() for c in _CAPTIONS) else v

    z1, z2 = _zone_name("line1_name"), _zone_name("line2_business_name")
    if z1 or z2:
        from .fields import _norm_name
        t1 = str(ext.fields.get("line1_name") or "").strip()
        t2 = str(ext.fields.get("line2_business_name") or "").strip()
        if (z1 and t1 and _norm_name(t1) == _norm_name(z2 or "")
                and _norm_name(t1) != _norm_name(z1)):
            # the text tier put Line 2's value into Line 1 — positional repair
            ext.warnings.append(
                "line1/line2: text model put the business name (Line 2) into "
                "Line 1 — repaired from the name-box crop; verify")
            ext.fields["line1_name"] = z1
            ext.fields["line2_business_name"] = z2 or t1
            ext.provenance["line1_name"] = {"source": "zone-probe", "page": probe_page}
            ext.provenance["line2_business_name"] = {"source": "zone-probe",
                                                     "page": probe_page}
        else:
            if z1 and not t1:
                ext.fields["line1_name"] = z1
                ext.provenance["line1_name"] = {"source": "zone-probe", "page": probe_page}
            elif z1 and t1 and _norm_name(z1) != _norm_name(t1):
                ext.warnings.append("line1_name: name-box crop disagrees with the "
                                    "text read — kept the text value; verify")
            if z2 and not t2:
                ext.fields["line2_business_name"] = z2
                ext.provenance["line2_business_name"] = {"source": "zone-probe",
                                                         "page": probe_page}


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


def _engine_compare(llm_fields: dict, raw: RawDoc, doc_class: str) -> list[dict]:
    """Dual-engine audit artifact: for every ID field, what did the DETERMINISTIC
    engine (OCR patterns) see vs the LLM read, and do they agree? Values are
    masked (this ships in run artifacts). agree=None when only one engine saw a
    value — informational, not a disagreement."""
    from .fields import _norm_id
    from .privacy import FIELD_KIND, mask
    pairs = (("iban", "iban"), ("swift_bic", "swift_bic"),
             ("account_number", "account_number"),
             ("routing_aba", "routing_aba"), ("routing_aba_wires", "routing_aba_wires")) \
        if doc_class == "bank" else \
        (("ein", "tin_raw"), ("tin_boxed", "tin_raw"), ("ssn", "tin_raw"))
    out: list[dict] = []
    for cand_key, field_key in pairs:
        det = str(raw.regex_candidates.get(cand_key) or "").strip()
        llm = str(llm_fields.get(field_key) or "").strip()
        if not det and not llm:
            continue
        kind = FIELD_KIND.get(field_key, "account_number")
        agree = None
        if det and llm:
            dn, ln = _norm_id(det), _norm_id(llm)
            agree = dn == ln or (field_key == "account_number"
                                 and dn.lstrip("0") == ln.lstrip("0") != "")
        out.append({"field": field_key, "candidate": cand_key,
                    "deterministic": mask(kind, det) if det else "",
                    "llm": mask(kind, llm) if llm else "",
                    "agree": agree,
                    "only": ("deterministic" if det and not llm
                             else "llm" if llm and not det else "")})
    return out


def extract(raw: RawDoc, quality: bool = False, policy: str = "masked",
            engine: str = "auto") -> Extraction:
    doc_class = raw.doc_class
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    no_llm = engine == "deterministic"
    ext_res = Extraction(doc_class=doc_class,
                         model_id="(deterministic — no LLM)" if no_llm else mc.resolve("TEXT"))
    ext_res.engine = engine
    ext_res.warnings = list(raw.warnings)

    # deterministic overrides that need no model
    if raw.editable:
        ext_res.doc_type = "editable_source"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}
    elif raw.ext in config.EMAIL_EXTS:
        ext_res.doc_type = "email"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}

    if raw.raw_text.strip() and not no_llm:
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
        if no_llm:
            ext_res.warnings.append(
                "deterministic engine: LLM extraction skipped — fields come from "
                "OCR patterns only; narrative fields may be empty")
        elif not raw.locked and not raw.editable:
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
    # a genuine invoice page ANYWHERE in a packet with NO bank confirmation letter
    # is disqualifying — an invoice is never acceptable as banking support (BNK-001,
    # REJECT), even when the deep-read pages are payment instructions. The survey
    # flags invoice_pages from every page's light text, so this catches an invoice
    # page even when the model only saw the bank-detail pages.
    elif (doc_class == "bank" and raw.invoice_pages and not raw.bank_letter_pages
          and ext_res.doc_type not in ("invoice",)):
        pages = ", ".join(str(p + 1) for p in raw.invoice_pages)
        ext_res.warnings.append(
            f"packet contains a genuine invoice page ({pages}) with its own invoice "
            f"number/date/amount — an invoice is never bank proof; the "
            f"payment-instructions/bank pages do not rescue it")
        ext_res.doc_type = "invoice"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": raw.invoice_pages[0] + 1}
    # deterministic type hints beat a hesitant model on hard-reject types
    elif raw.type_hint == "invoice" and ext_res.doc_type not in ("invoice",):
        ext_res.warnings.append(f"type hint 'invoice' overrides model '{ext_res.doc_type}'")
        ext_res.doc_type = "invoice"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}

    _ground_payment_instructions(ext_res, raw)

    # echo guards run BEFORE escalation: a dropped echo leaves a gap the strong
    # tier must be given the chance to fill
    _drop_exemplar_echo(ext_res, raw)
    _drop_filename_echo(ext_res, raw)
    _drop_regulator_noise(ext_res)
    _normalize_tin(ext_res)
    # dual engine: remember what the LLM said BEFORE the deterministic layer
    # overwrites it (the crosscheck makes regex the authority)
    llm_snapshot = dict(ext_res.fields)
    ext_res.crosscheck = crosscheck_ids(ext_res.fields, raw.regex_candidates,
                                        doc_class, policy=policy,
                                        prov=ext_res.provenance)
    # audit guards AFTER the crosscheck assignment (it REPLACES the list — notes
    # added earlier would be wiped) and before escalation (a dropped postal-code
    # 'account' must open the gap the strong tier is asked to fill)
    _audit_bank_ids(ext_res, raw)
    _fix_jp_form(ext_res, raw)
    _fix_statement_period(ext_res, raw)
    _esignature_guard(ext_res, raw)

    # --- escalation to the strong tier (quality first) -------------------------
    reasons = [] if no_llm else escalation_reasons(ext_res, raw, quality)
    if reasons and raw.raw_text.strip() and mc.strong_distinct():
        strong_obj, strong_ok, strong_model = _run_model(raw, "TEXT_STRONG",
                                                         focus=reasons)
        ext_res.strong_json_valid = strong_ok and strong_obj is not None
        if strong_obj is not None:
            strong_fields = _fields_from(strong_obj, keys)
            merged, tier_notes = _merge_tiers(ext_res.fields, strong_fields, keys, policy)
            ext_res.fields = merged
            ext_res.warnings += tier_notes
            # the strong tier reads the same filename-bearing prompt — re-guard
            _drop_exemplar_echo(ext_res, raw)
            _drop_filename_echo(ext_res, raw)
            _drop_regulator_noise(ext_res)
            strong_type = str(strong_obj.get("doc_type", "") or "").strip().lower()
            if (strong_type in types and not raw.editable
                    and raw.ext not in config.EMAIL_EXTS
                    and not (doc_class == "bank" and raw.bank_letter_pages
                             and strong_type == "invoice")
                    and not (doc_class == "bank" and raw.invoice_pages
                             and not raw.bank_letter_pages)
                    and raw.type_hint != "invoice"):
                ext_res.doc_type = strong_type
            _ground_payment_instructions(ext_res, raw)
            _normalize_tin(ext_res)
            llm_snapshot = dict(ext_res.fields)   # merged fast+strong LLM view
            # regex stays the highest authority — re-run the crosscheck on the merge
            ext_res.crosscheck = crosscheck_ids(ext_res.fields, raw.regex_candidates,
                                                doc_class, policy=policy,
                                                prov=ext_res.provenance)
            # re-audit AFTER the re-crosscheck (it replaced the notes again)
            _audit_bank_ids(ext_res, raw)
            _fix_jp_form(ext_res, raw)
            _fix_statement_period(ext_res, raw)
            _esignature_guard(ext_res, raw)
            ext_res.tier, ext_res.model_strong = "strong", strong_model
        else:
            ext_res.warnings.append("strong tier returned no valid JSON — fast result kept")
    ext_res.escalated_because = reasons

    _apply_w9_zone_probe(ext_res, raw)
    _normalize_tin(ext_res)          # zone TIN passes through the date guard too
    _apply_signature_probe(ext_res, raw)
    # the vision probe judges PIXELS and would overwrite an electronic signature
    # (DocuSign box reads as 'typed name') — the e-signature fact wins back here
    _esignature_guard(ext_res, raw)
    _finalize_provenance(ext_res, raw)

    if engine == "dual":
        ext_res.engine_compare = _engine_compare(llm_snapshot, raw, doc_class)

    ext_res.register_secrets()
    # regex candidates hold full values too — register so the leak gate knows them
    from .privacy import FIELD_KIND, _digits
    # TIN/banking kind-conflict guard: one digit string CANNOT be both a banking
    # identifier (shown in full under the operator display policy) and a TIN
    # (masked under EVERY policy) — the tin-only leak gate would then block the
    # run for 'leaking' the document's own routing/account digits. Real case: a
    # Latvian print '61-2612345' matched the EIN shape while the same nine
    # digits were the routing/account candidate. On a collision the BANKING
    # identity wins (bank ids drive this doc class; the ein detector is a W-9
    # tool) and the value is not registered as a TIN secret.
    bank_seqs = {_digits(str(v)) for k, v in raw.regex_candidates.items()
                 if k in ("iban", "account_number", "routing_aba", "routing_aba_wires")}
    bank_seqs |= {_digits(str(ext_res.fields.get(k) or ""))
                  for k in ("iban", "account_number", "routing_aba", "routing_aba_wires")}
    bank_seqs.discard("")

    def _collides_with_bank_id(value: str) -> bool:
        d = _digits(str(value))
        return bool(d) and any(d in b or b in d for b in bank_seqs)

    for k, v in raw.regex_candidates.items():
        if k in ("iban", "account_number", "routing_aba", "routing_aba_wires"):
            ext_res.vault.register(FIELD_KIND.get(k, "account_number"), v)
        elif k in ("ein", "tin_boxed"):
            if _collides_with_bank_id(v):
                _cross_note(ext_res, f"{k}-shaped candidate matches a banking "
                                     "identifier's digits — treated as a bank id, not a TIN")
            else:
                ext_res.vault.register(FIELD_KIND.get(k, "account_number"), v)
    return ext_res
