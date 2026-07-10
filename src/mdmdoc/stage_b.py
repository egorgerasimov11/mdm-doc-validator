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
from .fields import (BANK_DOC_TYPES, BANK_KEYS, W8_KEYS, W9_DOC_TYPES, W9_KEYS,
                     Extraction, crosscheck_ids, iban_mod97_ok, to_iso2)
from .fields import find_valid_ibans as fields_valid_ibans
from .stage_a import RawDoc

_DATE_SHAPE = re.compile(r"^\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$")


def _is_w8(raw: RawDoc) -> bool:
    """The W-8 subtype gate: same doc_class ('w9' — one rules file, one sniff,
    one BUT000 compare) but a DIFFERENT schema/prompt/report. type_hint is
    frozen on the pre-hunt text in perceive(), so this never flips mid-run."""
    return raw.doc_class == "w9" and raw.type_hint == "w8"


def _prompt_profile(doc_class: str, is_w8: bool = False) -> str:
    return "bank" if doc_class == "bank" else ("w8" if is_w8 else "w9")


def _load_system(doc_class: str, is_w8: bool = False) -> str:
    p = config.PROMPTS_DIR / f"system_{_prompt_profile(doc_class, is_w8)}.txt"
    return p.read_text() if p.exists() else ""


def _load_fewshot(doc_class: str, is_w8: bool = False) -> list[dict]:
    p = config.FEWSHOT_DIR / f"{_prompt_profile(doc_class, is_w8)}.json"
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
    "w8-no-name": (
        "PRIOR-PASS GAP: no beneficial-owner name. Part I Line 1 of a W-8 is "
        "'Name of organization that is the beneficial owner' (W-8BEN-E) or "
        "'Name of individual' (W-8BEN) — read it from Part I, never from an "
        "example or the withholding agent's name."),
    "w8-no-country": (
        "PRIOR-PASS GAP: no country. Part I Line 2 is 'Country of "
        "incorporation or organization' (W-8BEN-E) / 'Country of citizenship' "
        "(W-8BEN) — a country NAME, not an address."),
}


def build_prompt(raw: RawDoc, role: str = "TEXT", focus: list[str] | None = None) -> str:
    doc_class = raw.doc_class
    is_w8 = _is_w8(raw)
    keys = BANK_KEYS if doc_class == "bank" else (W8_KEYS if is_w8 else W9_KEYS)
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    parts = []
    # our custom model has the exemplars baked in via MESSAGE pairs; any stock
    # model (incl. the strong tier) gets them injected at runtime. W-8 exemplars
    # are NOT baked into mdmdoc-extract — inject them unconditionally.
    if is_w8 or not mc.resolve(role).startswith("mdmdoc-extract"):
        for ex in _load_fewshot(doc_class, is_w8):
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
    """One tier run -> (fields-bearing obj or None, json_first_try, model_id).
    The FAST tier's call is byte-frozen (trainable contract); effort 5 (D4) may
    widen ONLY the strong tier: extra options (num_predict) and the model's
    thinking channel, via runctl overrides."""
    options = {"num_ctx": 16384, "temperature": 0, "seed": 7}
    think = False
    if role == "TEXT_STRONG":
        from . import runctl
        options.update(runctl.override("strong_model_options") or {})
        think = bool(runctl.override("strong_think", False))
    obj, first_try = mc.generate_json(role, build_prompt(raw, role, focus=focus),
                                      system=_load_system(raw.doc_class, _is_w8(raw)),
                                      options=options, think=think)
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
    if ext.doc_class == "w9" and ext.doc_type == "w8" and "legal_name" in f:
        if not str(f.get("legal_name") or "").strip():
            r.append("w8-no-name")
        if not str(f.get("country_incorporation") or "").strip():
            r.append("w8-no-country")
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
    if ext.doc_class != "w9" or "tin_raw" not in ext.fields:
        return   # W-8 schema has no tin_raw/tin_type — nothing to normalize
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


def _baked_exemplar_values() -> set:
    """Exemplar values BAKED into the custom mdmdoc-extract model (snapshotted
    by modelfile.py at build time into models/exemplar_values.json). The live
    few-shot files drift away from what was baked — the echo guard was blind to
    baked-only values (real case: 'ACME' echoed into a W-8's line 1)."""
    p = config.MODELS_DIR / "exemplar_values.json"
    try:
        vals = json.loads(p.read_text())
        return {str(v).casefold() for v in vals if str(v).strip()}
    except Exception:
        return set()


def _exemplar_values(doc_class: str, exclude_sha: str = "") -> set:
    """All string values that appear in few-shot exemplar OUTPUTS — the live
    files (both schemas of the class) UNION the baked-model snapshot. These are
    shape-preserving fakes / example data — a real document can never
    legitimately contain them; if the model outputs one, it echoed the exemplar.
    Exemplars built from `exclude_sha` (the document being processed) are
    skipped: its own gold values are NOT echoes."""
    vals: set = set()
    shots = list(_load_fewshot(doc_class))
    if doc_class == "w9":   # the w8 subtype exemplars ride the same class
        shots += _load_fewshot(doc_class, is_w8=True)
    for ex in shots:
        if exclude_sha and ex.get("doc_sha256") == exclude_sha:
            continue
        out = ex.get("output", {})
        for v in (out.get("fields") or {}).values():
            s = str(v or "").strip()
            if len(s) >= 4 and not isinstance(v, bool):
                vals.add(s.casefold())
    return vals | _baked_exemplar_values()


# classic tutorial placeholders a model reaches for when it did NOT read the
# document — never legitimate NAME-field values unless literally printed there
_PLACEHOLDER_NAMES = frozenset({
    "acme", "acme corp", "acme corporation", "acme inc", "acme llc",
    "john doe", "jane doe", "john smith", "jane smith", "example corp",
    "example company", "test company", "sample company", "company name",
    "your company", "n/a",
})
_ECHO_NAME_FIELDS = ("line1_name", "line2_business_name", "account_holder",
                     "bank_name", "legal_name", "signer_name")


def _drop_exemplar_echo(ext: Extraction, raw: RawDoc) -> None:
    """Real case: a W-8 came back with 'ACME' and an exemplar's fake EIN —
    the model copied the few-shot example instead of reading the document.
    Any extracted value that equals an exemplar value (live few-shot files or
    the baked-model snapshot) AND does not occur in the document text is an
    echo — drop it. NAME fields additionally pass a placeholder denylist:
    'ACME'/'John Doe' without support in the text is invented, whatever
    exemplar set it came from."""
    exemplar_vals = _exemplar_values(ext.doc_class, exclude_sha=raw.sha256)
    doc_text = raw.raw_text.casefold()
    for k, v in list(ext.fields.items()):
        if isinstance(v, bool):
            continue
        s = str(v or "").strip()
        if not s or s.casefold() in doc_text:
            continue
        if s.casefold() in exemplar_vals:
            ext.fields[k] = ""
            ext.provenance.pop(k, None)
            ext.warnings.append(f"{k}: dropped few-shot exemplar echo (value was "
                                "copied from an example, not read from the document)")
        elif k in _ECHO_NAME_FIELDS and s.casefold() in _PLACEHOLDER_NAMES:
            ext.fields[k] = ""
            ext.provenance.pop(k, None)
            ext.warnings.append(f"{k}: dropped placeholder value '{s}' — not "
                                "present in the document text")


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
    cands = getattr(raw, "regex_candidates", {}) or {}
    nc_cand = re.sub(r"\D", "", str(cands.get("national_clearing") or ""))
    for k in ("routing_aba", "routing_aba_wires"):
        d = re.sub(r"\D", "", str(f.get(k) or ""))
        if d and len(d) != 9:
            f[k] = ""
            ext.provenance.pop(k, None)
            # a labeled national clearing code the model pressed into a routing
            # field is REAL data — relocate it instead of demoting it to a note
            if (nc_cand and d == nc_cand
                    and not str(f.get("national_clearing") or "").strip()):
                f["national_clearing"] = str(cands["national_clearing"])
                f["national_clearing_kind"] = str(
                    cands.get("national_clearing_kind") or "")
                ext.provenance["national_clearing"] = {"source": "rule", "page": None}
                _cross_note(ext, "non-ABA routing value relocated to the national "
                                 f"clearing field ({f['national_clearing_kind'] or 'domestic'})")
                continue
            domestic.append(d)
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
        # values through the run's display policy — this note used to embed the
        # raw digits, which breached masking and aborted api-only runs (C6)
        from .privacy import display_value
        _cross_note(ext, "account number: printed form "
                         f"{display_value('account_number', printed, ext.policy)} "
                         "(IBAN account part is the zero-padded "
                         f"{display_value('account_number', acct, ext.policy)})")


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


def _fix_zh_form(ext: Extraction, raw: RawDoc) -> None:
    """Chinese domestic forms/notices (mirror of _fix_jp_form): an 11-digit CN
    MOBILE number (1[3-9]xxxxxxxxx) is not an account number; labeled 账号/账户
    fields rescue the real value; country is inferable. NFKC first (full-width
    digits). Gated on CN markers and the ABSENCE of the JP 口座 marker."""
    if ext.doc_class != "bank":
        return
    import unicodedata
    text = unicodedata.normalize("NFKC", raw.raw_text or "")
    if not re.search(r"账|帐|户名|开户", text) or "口座" in text:
        return
    f = ext.fields
    acct = re.sub(r"\D", "", str(f.get("account_number") or ""))
    # [CONST:zh_mobile_shape]
    if re.fullmatch(r"1[3-9]\d{9}", acct):
        pos = text.find(acct)
        ctx = text[max(0, pos - 24):pos + len(acct) + 8] if pos >= 0 else ""
        # [CONST:zh_phone_context_labels]
        if pos < 0 or re.search(r"电话|手机|联系人|联系方式|传真|邮编", ctx):
            ext.warnings.append(f"account_number …{acct[-4:]} matches the CN mobile "
                                "shape in a phone/contact context — dropped")
            f["account_number"] = ""
            ext.provenance.pop("account_number", None)
    if not str(f.get("account_number") or "").strip():
        # [CONST:zh_account_labels]
        m = re.search(r"(?:开户账号|收款账[户号]|银行账[户号]|账号|帐号|账户)"
                      r"\s*[:：]?\s*([0-9][0-9 \-]{6,28}[0-9])", text)
        if m:
            f["account_number"] = re.sub(r"[ \-]", "", m.group(1))
            ext.provenance["account_number"] = {"source": "ocr-regex", "page": None}
            _cross_note(ext, "account number taken from the labeled 账号/账户 field")
    if not str(f.get("bank_country") or "").strip():
        f["bank_country"] = "CN"
        ext.provenance["bank_country"] = {"source": "rule", "page": None}
        _cross_note(ext, "bank country inferred: CN (Chinese domestic form markers)")


def _ground_national_clearing(ext: Extraction, raw: RawDoc) -> None:
    """A labeled national clearing code (CN CNAPS 联行号, IN IFSC, UK sort code,
    AU BSB, DE BLZ) is the domestic analog of a US routing number — and for
    CN/GB/AU/IN it IS the SAP Bank Key. Real case: a Chinese letter's 联行号
    303100000397 was invisible to every field, so the SAP bank-key compare
    always fell to 'sap-only'. Derived field outside BANK_KEYS (the model
    prompt never changes); never overwrites an existing value."""
    if ext.doc_class != "bank":
        return
    cand = str(raw.regex_candidates.get("national_clearing") or "").strip()
    if not cand or str(ext.fields.get("national_clearing") or "").strip():
        return
    from .privacy import display_value
    kind = str(raw.regex_candidates.get("national_clearing_kind") or "").strip()
    ext.fields["national_clearing"] = cand
    ext.fields["national_clearing_kind"] = kind
    ext.provenance["national_clearing"] = {"source": "ocr-regex", "page": None}
    _cross_note(ext, f"national clearing code ({kind or 'domestic'}) taken from "
                     f"the labeled field: {display_value('routing_aba', cand, ext.policy)}")


# role labels that mark a NAME as signer/contact — not the account OWNER
# [CONST:signatory_labels]
_SIGNATORY_LABEL_RE = re.compile(
    r"(?i)\b(?:account\s+signator(?:y|ies)|authori[sz]ed\s+sign(?:er|ers|atory|atories)|"
    r"signator(?:y|ies)|contact\s+(?:person|name)|representative|attn\.?|attention|"
    r"prepared\s+by|requested\s+by|submitted\s+by)\b")
# holder labels that KEEP the value where it is (ownership context wins)
_HOLDER_KEEP_RE = re.compile(
    r"(?i)account\s*holder|beneficiary|account\s*name|in\s+the\s+name\s+of|"
    r"a\s+nombre\s+de|titular|kontoinhaber|intestat")
# relationship sentences that NAME the owner ('verify that X is a customer')
_REL_HOLDER_RES = (
    re.compile(r"(?i)\b(?:verify|verifies|confirm|confirms|certify|certifies)\s+that\s+"
               r"([A-Z][\w&.,'()\- ]{2,70}?)\s+is\s+(?:a|an|our)\s+"
               r"(?:valued\s+)?(?:customer|client|account\s*holder)"),
    re.compile(r"(?i)\b([A-Z][\w&.,'()\- ]{2,60}?)\s+(?:holds|maintains)\s+"
               r"(?:a|an|the|its)?\s*(?:(?:business|checking|deposit)\s+)*account"),
)


def _ground_account_holder(ext: Extraction, raw: RawDoc) -> None:
    """Role gate for account_holder (bank): a name printed under a
    signatory/contact label is the SIGNER, not the account owner (real case:
    'Account signatory: <person>' became the holder and the run ACCEPTed).
    (a) signatory-labeled value moves to the derived account_signatory field;
    (b) an empty holder is re-grounded from a relationship sentence
    ('...verify that <NAME> is a customer...'); (c) ownership-labeled values
    are never touched."""
    if ext.doc_class != "bank":
        return
    f = ext.fields
    holder = str(f.get("account_holder") or "").strip()
    text = raw.raw_text or ""
    if holder:
        ctx = _value_line_context(text, holder)
        if ctx and _SIGNATORY_LABEL_RE.search(ctx) and not _HOLDER_KEEP_RE.search(ctx):
            f["account_signatory"] = holder
            f["account_holder"] = ""
            ext.provenance.pop("account_holder", None)
            ext.provenance["account_signatory"] = {"source": "rule", "page": None}
            ext.warnings.append(
                f"account_holder '{holder}' sits under a signatory/contact label — "
                "moved to account_signatory (a signer is not the account owner)")
            holder = ""
    if not holder:
        for rx in _REL_HOLDER_RES:
            m = rx.search(text)
            if not m:
                continue
            name = m.group(1).strip().strip(".,;:'\"")
            if 3 <= len(name) <= 70:
                f["account_holder"] = name
                ext.provenance["account_holder"] = {"source": "rule", "page": None}
                _cross_note(ext, "account holder grounded from the document's "
                                 "relationship sentence")
                break


def _value_line_context(text: str, value: str) -> str:
    """The line carrying `value` plus the line above it — labels commonly sit
    on either. Empty string when the value is not printed as-is."""
    vcf = value.casefold()
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if vcf in ln.casefold():
            return (lines[i - 1] + "\n" + ln) if i > 0 else ln
    return ""


def _record_settlement_issuer(ext: Extraction, raw: RawDoc) -> None:
    """Issuer-aware evidence for payment_instructions (G3): bank-ISSUED
    standard settlement instructions (named bank + officer block or an
    account-held statement) are a different beast from a supplier's self-made
    ACH sheet. Records the component booleans and the settlement_issuer_strong
    flag that BNK-027 (NOTE, pending approval) reads — never a verdict."""
    if ext.doc_class != "bank" or ext.doc_type != "payment_instructions":
        return
    from . import doctype_evidence
    comp = doctype_evidence.settlement_issuer(raw.raw_text, raw.regex_candidates,
                                              ext.fields.get("officer_block"))
    ext.doc_subtype_evidence = {"settlement_issuer": comp}
    if comp["issuer_strong"]:
        ext.fields["settlement_issuer_strong"] = True
        ext.provenance["settlement_issuer_strong"] = {"source": "rule", "page": None}


def _collect_inventory(ext: Extraction, raw: RawDoc) -> None:
    """'Also on the document' (O2): label-anchored inventory of identifiers the
    schema does not carry — tax IDs, company registrations, every labeled
    account occurrence, clearing codes, phone/email/address. Nothing visible is
    dropped silently anymore. Sensitive values are vault-registered (the CN
    taxpayer ID …233T was previously NOT leak-gated at all) and stored
    masked/policy-shown; the multi-block account logic flags two DIFFERENT
    labeled accounts (BNK-031) or corroborates a repeated one."""
    from . import inventory as inv
    from .fields import _norm_id
    from .privacy import TIN_KINDS, display_value, mask
    items = inv.collect(raw.raw_text or "")
    if not items:
        return
    pub_items: list[dict] = []
    acct_norms: list[str] = []
    for it in items:
        v, kind = it["value"], it["kind"]
        if kind == "account_number":
            acct_norms.append(_norm_id(v))
        if kind in TIN_KINDS:
            ext.vault.register(kind, v)
            shown = mask(kind, v)
        elif kind:
            ext.vault.register(kind, v)
            shown = display_value(kind, v, ext.policy)
        else:
            shown = v
        pub_items.append({"family": it["family"], "label": it["label"],
                          "value": shown})
    ext.inventory = pub_items

    # multi-block account signal: IBAN-shaped values and values contained in
    # the extracted IBAN / clearing code are excluded — a European letter that
    # prints IBAN + Konto is ONE account, not two
    iban_norm = _norm_id(ext.fields.get("iban") or "")
    nc_norm = _norm_id(ext.fields.get("national_clearing") or "")
    nums: list[str] = []
    for a in acct_norms:
        if not a or not a.isdigit():
            continue
        if nc_norm and a == nc_norm:
            continue
        if iban_norm and (a == iban_norm or iban_norm.endswith(a.lstrip("0"))):
            continue
        nums.append(a.lstrip("0"))
    extracted = _norm_id(ext.fields.get("account_number") or "").lstrip("0")
    if len(set(nums)) >= 2:
        ext.fields["distinct_accounts"] = True
        ext.provenance["distinct_accounts"] = {"source": "rule", "page": None}
        ext.warnings.append(
            "document carries two or more DIFFERENT labeled account numbers — "
            "verify which account is actually being requested")
    elif extracted and nums.count(extracted) >= 2:
        _cross_note(ext, f"account number corroborated by {nums.count(extracted)} "
                         "labeled occurrences in the document")


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
    """An e-signature envelope/annotation IS a signature (kind=electronic) —
    'unsigned' would be wrong. Applies to bank AND w9/w8 (S1: a W-9 with a
    textual 'Digitally signed by …' line used to false-fire W9-020 because
    this guard was bank-only). Tokens shared with the text vote (fields.
    ESIGN_TOKENS); the timestamp near the marker is the signing date."""
    if ext.fields.get("signed"):
        return
    text_low = (raw.raw_text or "").lower()
    from .fields import ESIGN_TOKENS
    if not any(t in text_low for t in ESIGN_TOKENS):
        return
    ext.fields["signed"] = True
    ext.fields["signature_kind"] = "electronic"
    ext.provenance["signed"] = {"source": "rule", "page": None}
    if ext.doc_class == "bank":
        from .rules.predicates import _positive_evidence
        if not _positive_evidence(str(ext.fields.get("signature_evidence") or "")):
            ext.fields["signature_evidence"] = \
                "electronically signed (e-signature markers in document text)"
        if not str(ext.fields.get("doc_date") or "").strip():
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s*\|\s*\d{1,2}:\d{2}", raw.raw_text or "")
            if m:
                ext.fields["doc_date"] = m.group(1)
                ext.provenance["doc_date"] = {"source": "ocr-regex", "page": None}
    else:  # w9 / w8: the annotation timestamp is the signing date
        if not str(ext.fields.get("sign_date") or "").strip():
            m = re.search(r"(\d{4}[.\-/]\d{2}[.\-/]\d{2})", raw.raw_text or "")
            if m:
                ext.fields["sign_date"] = m.group(1)
                ext.provenance["sign_date"] = {"source": "ocr-regex", "page": None}


def _officer_block_guard(ext: Extraction, raw: RawDoc) -> None:
    """Detect a typed officer block (sign-off line + name + title + contact) —
    compensating evidence for a system-issued unsigned bank letter (BNK-026).
    Detection only; the evidence fill happens in _resolve_signature AFTER the
    final signed state is known (S1)."""
    if ext.doc_class != "bank":
        return
    from .fields import detect_officer_block
    fired, snippet = detect_officer_block(raw.raw_text or "")
    if fired:
        ext.fields["officer_block"] = True
        ext.officer_snippet = snippet


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
    # Evidence rescue (П3/R1): when the page carries OVERWHELMING deterministic
    # bank-letter evidence (letter shape AND bank identity AND account facts
    # AND holder signal — every component required), the model's ungrounded
    # payment_instructions call is a mislabel of a real letter; classify
    # bank_letter but FLAG it (doc_type_uncertain -> confidence weak signal),
    # so a shaky ACCEPT can still be held by CONF-001. Weaker evidence keeps
    # the conservative grounding below (the CJK exhibition-notice case has
    # bank tokens + account digits but no letter shape/holder -> unchanged).
    if config.doctype_rescue_enabled():
        from .doctype_evidence import score
        sc = score(raw.raw_text, raw.regex_candidates, raw.bank_letter_pages)
        if sc["bank_letter_strong"]:
            ext.warnings.append(
                "model said payment_instructions; deterministic evidence (letter "
                "shape + bank identity + account facts + holder signal) says "
                "bank_letter — classified bank_letter (uncertain)")
            ext.doc_type = "bank_letter"
            ext.provenance["doc_type"] = {"source": "rule", "page": None}
            ext.doc_type_uncertain = True
            ext.doc_type_evidence = dict(sc["components"])
            return
    # ALWAYS degrade to 'other' — never to the type hint. A weak hint can be
    # STRONGER-accepting than the ungrounded claim (live case: 开户银行 on the
    # organiser's remittance block hinted bank_letter, which would have turned
    # an exhibition notice into an auto-ACCEPTed bank confirmation). 'other'
    # routes to the missing-holder/no-bank-ids review rules — conservative.
    ext.warnings.append(
        "model classified payment_instructions but the document carries no "
        "payment-instruction markers — classified as other")
    ext.doc_type = "other"
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


def _resolve_signature(ext: Extraction, raw: RawDoc) -> None:
    """The single fold point for every signature channel (S1): text e-sign
    markers, the vision probe, the typed-officer block. Sets `signed` and
    `signature_kind` ∈ {wet, stamp, electronic, none, unverified}.

    NO-OVERWRITE RULE: a NEGATIVE probe never touches signature_evidence —
    that field feeds BNK-021/026 through _positive_evidence, and the real
    Citizens regression came from the probe's "no handwritten signature"
    string erasing the text-tier's typed-officer evidence."""
    from .fields import ESIGN_TOKENS, TYPED_SYSTEM_TOKENS
    from .privacy import scrub_text
    from .rules.predicates import _positive_evidence

    low = (raw.raw_text or "").lower()
    text_vote = ("esign" if any(t in low for t in ESIGN_TOKENS)
                 else "typed-system" if any(t in low for t in TYPED_SYSTEM_TOKENS)
                 else "none")
    probe = raw.signature_probe or {}
    votes = probe.get("votes") or {}

    def _pub(extra: dict | None = None) -> None:
        trail = [{**t, "evidence": scrub_text(str(t.get("evidence") or ""), ext.vault)}
                 for t in (probe.get("trail") or [])]
        ext.signature_probe = {
            "handwritten_signature": bool(probe.get("handwritten_signature")),
            "stamp": bool(probe.get("stamp")),
            "evidence": scrub_text(str(probe.get("evidence") or ""), ext.vault),
            "page": (probe.get("page", 0) + 1
                     if isinstance(probe.get("page"), int) else None),
            "votes": votes, "trail": trail,
            "uncertain": bool(probe.get("uncertain")),
            "contested": bool(probe.get("contested")),
            "escalated": bool(probe.get("escalated")),
            "probes_used": probe.get("probes_used", 0),
            "esign_short_circuit": bool(probe.get("esign_short_circuit")),
            **(extra or {})}

    # (E) electronic: the esign guard fired, or the text carries esign markers
    if ext.fields.get("signature_kind") == "electronic" or text_vote == "esign":
        ext.fields["signed"] = True
        ext.fields["signature_kind"] = "electronic"
        ext.provenance.setdefault("signed", {"source": "rule", "page": None})
        _pub({"uncertain": False})
        _finish_signature(ext, text_vote, _positive_evidence)
        return

    # (P0) no probe ran (invoice/editable/locked/no-vision): text tier stands
    if not probe:
        ext.fields.setdefault("signature_kind", "unverified")
        _finish_signature(ext, text_vote, _positive_evidence)
        return

    # (P1) vision attempted but unusable: keep the text-tier signed, flag it
    if probe.get("no_visual_verdict"):
        ext.fields["signature_kind"] = "unverified"
        _pub({"evidence": "", "page": None, "uncertain": True,
              "no_visual_verdict": True})
        ext.warnings.append("signature vision produced no usable read — signature "
                            "state unverified (text-tier value kept); verify by eye")
        _finish_signature(ext, text_vote, _positive_evidence)
        return

    # (scoped-out) the probe landed on a W-9 page of a bank packet: its
    # signature belongs to the tax form, not the letter (P3/D backstop)
    probe_idx = probe.get("page")
    if (ext.doc_class == "bank" and isinstance(probe_idx, int)
            and probe_idx in (raw.w9_pages or [])
            and any(p not in raw.w9_pages for p in raw.pages_used)):
        ext.fields["signature_kind"] = "unverified"
        _pub({"uncertain": True, "scoped_out_w9_page": True})
        ext.warnings.append(
            "signature probe landed on the W-9 page of the packet — its "
            "signature belongs to the W-9 section, not the bank document; "
            "signed kept from the text tier")
        _finish_signature(ext, text_vote, _positive_evidence)
        return

    probe_page = probe.get("page", 0) + 1 if isinstance(probe.get("page"), int) else None
    # a stamp whose evidence is a REGULATOR watermark (VIGILADO / Superintendencia
    # margin plate on Colombian letters) is compliance noise, not a signature —
    # same noise list that guards bank_name (deterministic backstop; the prompt
    # also says so, but the model must not be trusted alone here)
    ev_low = str(probe.get("evidence") or "").lower()
    stamp_ok = bool(probe.get("stamp")) and not any(n in ev_low for n in _BANKNAME_NOISE)
    if probe.get("stamp") and not stamp_ok:
        ext.warnings.append("signature probe: the 'stamp' evidence is a regulator "
                            "watermark — not counted as a signature")
    hand = bool(probe.get("handwritten_signature"))
    model_said = ext.fields.get("signed")
    model_said = model_said if isinstance(model_said, bool) else \
        str(model_said).lower() in ("true", "yes")
    evidence = scrub_text(str(probe.get("evidence") or ""), ext.vault)

    if hand:                                   # (P2) wet signature seen
        ext.fields["signed"] = True
        ext.fields["signature_kind"] = "wet"
        ext.provenance["signed"] = {"source": "vision-crop", "page": probe_page}
        if evidence:
            ext.fields["signature_evidence"] = evidence
            ext.provenance["signature_evidence"] = {"source": "vision-crop",
                                                    "page": probe_page}
        sig_date = str(probe.get("date_near_signature") or "").strip()
        if sig_date and ext.doc_class in ("w9",) \
                and not str(ext.fields.get("sign_date") or "").strip():
            ext.fields["sign_date"] = sig_date
            ext.provenance["sign_date"] = {"source": "vision-crop", "page": probe_page}
    elif stamp_ok:                             # (P3) stamp only
        ext.fields["signature_kind"] = "stamp"
        if ext.doc_class == "bank":
            ext.fields["signed"] = True
            ext.provenance["signed"] = {"source": "vision-crop", "page": probe_page}
            if evidence:
                ext.fields["signature_evidence"] = evidence
                ext.provenance["signature_evidence"] = {"source": "vision-crop",
                                                        "page": probe_page}
        else:
            ext.fields["signed"] = False
            ext.warnings.append("a stamp is not a signature on a W-9/W-8")
    else:                                      # (P4) vision says NO wet/stamp
        ext.fields["signed"] = False
        ext.fields["signature_kind"] = "none"
        ext.provenance["signed"] = {"source": "vision-crop", "page": probe_page}
        # NO-OVERWRITE: signature_evidence deliberately untouched here

    signed_now = bool(ext.fields.get("signed"))
    if signed_now != model_said:
        ext.warnings.append(f"signature: vision says {signed_now}, "
                            f"text model said {model_said}")
    _pub()
    if probe.get("uncertain"):
        ext.warnings.append(
            f"signature vision uncertain (band={votes.get('band', '?')}, "
            f"page={votes.get('page', '?')}, text={votes.get('text', '?')}) "
            "— verify by eye")
    _finish_signature(ext, text_vote, _positive_evidence)


def _finish_signature(ext: Extraction, text_vote: str, _positive_evidence) -> None:
    """Final compensating-evidence fill, AFTER the signed state is settled:
    an unsigned bank letter with a detected typed officer block (or an
    explicit computer-generated notice) gets BNK-026-grade evidence — unless
    the text tier already carries positive evidence (never overwrite it)."""
    signed = ext.fields.get("signed")
    signed = signed if isinstance(signed, bool) else \
        str(signed).lower() in ("true", "yes")
    ext.fields["signed"] = signed
    ext.fields.setdefault("signature_kind",
                          "wet" if signed else "none")
    if ext.doc_class != "bank" or signed:
        return
    cur = str(ext.fields.get("signature_evidence") or "")
    if _positive_evidence(cur):
        return
    if ext.fields.get("officer_block"):
        snippet = getattr(ext, "officer_snippet", "")
        ext.fields["signature_evidence"] = \
            f"typed officer block: {snippet}" if snippet else "typed officer block"
        ext.provenance["signature_evidence"] = {"source": "rule", "page": None}
    elif text_vote == "typed-system":
        ext.fields["signature_evidence"] = \
            "computer-generated notice (states no signature required)"
        ext.provenance["signature_evidence"] = {"source": "rule", "page": None}


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


# critical IDs the cross-page corroboration guard hunts (P5)
_CORROB_FIELDS = ("iban", "account_number", "routing_aba", "routing_aba_wires",
                  "swift_bic", "tin_raw")


def _page_class(text: str) -> str:
    """Coarse page class from the deterministic markers — corroboration only
    counts pages of DIFFERENT classes as independent."""
    from .fields import page_markers
    m = page_markers(text)
    if m["bank_letter"]:
        return "bank_letter"
    if m["w9_form"]:
        return "w9"
    if m["invoice"]:
        return "invoice"
    return "plain"


def _corroborate_across_pages(ext: Extraction, raw: RawDoc) -> None:
    """A critical ID printed on >=2 pages of DIFFERENT page classes (a letter
    AND a supplier sheet, say) was produced twice by independent layouts —
    stronger than the model==regex 'confirmed' echo, which reads ONE text.
    Marks provenance.confirmed_independent; the note carries page counts and
    classes only, never the ID itself (strict-gate safe)."""
    from .fields import _norm_id
    texts: dict[int, str] = {}
    for i in set(raw.page_texts) | set(raw.survey_texts):
        t = raw.page_texts.get(i) or raw.survey_texts.get(i) or ""
        if t.strip():
            texts[i] = t
    if len(texts) < 2:
        return
    norms = {i: _norm_id(t) for i, t in texts.items()}
    for k in _CORROB_FIELDS:
        s_id = _norm_id(str(ext.fields.get(k) or "").strip())
        if len(s_id) < 6:
            continue
        hits = [i for i in sorted(texts) if s_id in norms[i]]
        if len(hits) < 2:
            continue
        classes = sorted({_page_class(texts[i]) for i in hits})
        if len(classes) < 2:
            continue
        prov = ext.provenance.setdefault(k, {"source": "model", "page": None})
        prov["confirmed_independent"] = True
        _cross_note(ext, f"{k}=confirmed independently on {len(hits)} pages "
                         f"({'+'.join(classes)})")


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
            engine: str = "auto", injected_llm: dict | None = None) -> Extraction:
    """injected_llm (deterministic engine only): simulated model fields seeded
    into the extraction so guards/crosscheck run over them — the exact Python
    mirror of ABAP `build(it_llm_fields)`. Used by the golden parity corpus;
    ignored on every LLM-backed engine."""
    doc_class = raw.doc_class
    is_w8 = _is_w8(raw)
    keys = BANK_KEYS if doc_class == "bank" else (W8_KEYS if is_w8 else W9_KEYS)
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    no_llm = engine == "deterministic"
    ext_res = Extraction(doc_class=doc_class,
                         model_id="(deterministic — no LLM)" if no_llm else mc.resolve("TEXT"))
    ext_res.engine = engine
    ext_res.policy = policy
    ext_res.warnings = list(raw.warnings)

    # deterministic overrides that need no model
    if raw.editable:
        ext_res.doc_type = "editable_source"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}
    elif raw.ext in config.EMAIL_EXTS:
        ext_res.doc_type = "email"
        ext_res.provenance["doc_type"] = {"source": "rule", "page": None}
    elif is_w8:
        # the subtype gate already proved W-8 markers deterministically —
        # the model reads the W-8 schema and must not re-classify
        ext_res.doc_type = "w8"
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
        if no_llm and injected_llm is not None:
            # golden-parity seam: seed simulated model fields (doc_type stays
            # deterministic — mirrors ABAP where doc_type is a build() input)
            ext_res.fields = {k: injected_llm.get(k, "") for k in keys}
        else:
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
    _fix_zh_form(ext_res, raw)
    _ground_national_clearing(ext_res, raw)
    _ground_account_holder(ext_res, raw)
    _fix_statement_period(ext_res, raw)
    _esignature_guard(ext_res, raw)

    # --- escalation to the strong tier (quality first) -------------------------
    reasons = [] if no_llm else escalation_reasons(ext_res, raw, quality)
    from . import runctl
    if (reasons and raw.raw_text.strip() and mc.strong_distinct()
            and runctl.override("strong_enabled", True)):
        runctl.checkpoint("strong-tier", 55)
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
            if (strong_type in types and not raw.editable and not is_w8
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
            _fix_zh_form(ext_res, raw)
            _ground_national_clearing(ext_res, raw)
            _ground_account_holder(ext_res, raw)
            _fix_statement_period(ext_res, raw)
            _esignature_guard(ext_res, raw)
            ext_res.tier, ext_res.model_strong = "strong", strong_model
        else:
            ext_res.warnings.append("strong tier returned no valid JSON — fast result kept")
    ext_res.escalated_because = reasons

    _apply_w9_zone_probe(ext_res, raw)
    _normalize_tin(ext_res)          # zone TIN passes through the date guard too
    # signature channels fold ONCE at the tail (S1): officer-block detection
    # first (flag only), then _resolve_signature settles signed/kind across
    # text esign markers, the vision probe and the compensating evidence —
    # a negative probe can no longer overwrite the text-tier evidence
    _officer_block_guard(ext_res, raw)
    _resolve_signature(ext_res, raw)
    _record_settlement_issuer(ext_res, raw)   # needs the officer_block flag
    _collect_inventory(ext_res, raw)
    _corroborate_across_pages(ext_res, raw)
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
