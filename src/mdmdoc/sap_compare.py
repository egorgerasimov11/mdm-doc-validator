#!/usr/bin/env python3
"""
sap_compare.py — compare a validated banking document against the SAP MDG/Fiori
"Bank Details" screen. Today the SAP side comes from a SCREENSHOT (vision model
reads the screen); in a future BTP integration the same compare() runs against
live SAP field values — the comparison logic is source-agnostic.

Full values are compared in memory only; every persisted/rendered artifact
holds masked values plus the first-mismatch position.
"""
from __future__ import annotations

from pathlib import Path

from . import config, model_client as mc
from .fields import Extraction, _norm_id, _norm_name, names_materially_equal, to_iso2
from .privacy import FIELD_KIND, mask
from .rules.engine import Finding

SAP_KEYS = ["bank_details_id", "bank_account", "account_holder", "iban", "account_name",
            "control_key", "reference_details", "bank_country", "bank_key", "bank_name",
            "street", "city", "bank_branch", "swift_bic"]

SAP_PROMPT = (
    "This is a screenshot of an SAP MDG / Fiori 'Bank Details' screen. Read the form "
    "field VALUES exactly as displayed (copy character-for-character, keep leading "
    "zeros) and return JSON with keys: " + ", ".join(SAP_KEYS) + ". "
    "bank_country = the ISO code shown in Bank Country/Region (e.g. PL). "
    "Use \"\" for empty fields. Do not invent values."
)

_SENSITIVE_SAP = {"bank_account": "account_number", "iban": "iban"}


def _downscaled(image_path: Path):
    """The SAP screenshot is user-supplied and often a 2x Retina capture. Image
    tokens count against the vision context window — a raw 3420px capture is
    ~4k tokens and overflowed Ollama's context, silently killing the compare.
    Downscale to VISION_MAX_SIDE like every other vision path; returns a PIL
    image, or None when the original is already small enough. The inbox
    original is never modified."""
    from PIL import Image
    im = Image.open(image_path)
    if max(im.size) <= config.VISION_MAX_SIDE:
        return None
    f = config.VISION_MAX_SIDE / max(im.size)
    return im.convert("RGB").resize((int(im.width * f), int(im.height * f)),
                                    Image.LANCZOS)


def read_sap_screen(image_path: Path, vault) -> dict:
    """Vision-read the SAP screenshot -> {field: value}. Full values in memory;
    sensitive ones registered in the run's vault so scrub/leak-gate cover them."""
    import tempfile
    im = _downscaled(image_path)
    if im is None:
        obj, _ = mc.generate_json_vision(SAP_PROMPT, [str(image_path)])
    else:
        # temp file lives only for the duration of the call (pixels are sensitive)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sap_screen.png"
            im.save(p)
            obj, _ = mc.generate_json_vision(SAP_PROMPT, [str(p)])
    if not isinstance(obj, dict):
        return {}
    sap = {k: str(obj.get(k, "") or "").strip() for k in SAP_KEYS}
    for k, kind in _SENSITIVE_SAP.items():
        if sap.get(k):
            vault.register(kind, sap[k])
    return sap


def _first_diff(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i + 1
    return min(len(a), len(b)) + 1


def _row(field: str, doc_val: str, sap_val: str, status: str, note: str = "",
         kind: str | None = None, policy: str = "masked") -> dict:
    from .privacy import display_value
    dm = display_value(kind, doc_val, policy) if kind and doc_val else (doc_val or "—")
    sm = display_value(kind, sap_val, policy) if kind and sap_val else (sap_val or "—")
    return {"field": field, "doc": dm, "sap": sm, "status": status, "note": note}


def compare(ext: Extraction, sap: dict, policy: str = "masked") -> tuple[list[Finding], list[dict]]:
    """Deterministic char-by-char comparison -> (findings, display-policy rows)."""
    from .privacy import display_value

    def _dv(kind, v):
        return display_value(kind, v, policy)

    def _row_p(*a, **kw):
        return _row(*a, policy=policy, **kw)

    f = ext.fields
    findings: list[Finding] = []
    rows: list[dict] = []

    def crit(rule_id: str, msg: str) -> None:
        findings.append(Finding(rule_id, "CRITICAL", "NEED_MANUAL_REVIEW", msg))

    def warn(rule_id: str, msg: str) -> None:
        findings.append(Finding(rule_id, "WARNING", "WARNING", msg))

    # SAP frequently stores a DECOMPOSED IBAN (BANKL = bank code + BANKN = account
    # number, IBAN column itself empty — the normal BUT0BK table-export shape).
    # Detect when the document IBAN is consistent with that decomposition so we
    # confirm the fields instead of warning about a "missing" SAP IBAN.
    d_iban, s_iban = _norm_id(f.get("iban")), _norm_id(sap.get("iban"))
    s_key0, s_acc0 = _norm_id(sap.get("bank_key")), _norm_id(sap.get("bank_account"))
    key_in_doc_iban = bool(d_iban and s_key0
                           and s_key0 in d_iban[4:4 + len(s_key0) + 2])
    acc_in_doc_iban = bool(d_iban and s_acc0 and s_acc0.lstrip("0")
                           and d_iban.endswith(s_acc0.lstrip("0")))
    iban_decomposed = (not s_iban) and (key_in_doc_iban or acc_in_doc_iban)

    # IBAN — strict char-by-char
    if d_iban and s_iban:
        if d_iban == s_iban:
            rows.append(_row_p("IBAN", d_iban, s_iban, "match", kind="iban"))
        else:
            pos = _first_diff(d_iban, s_iban)
            rows.append(_row_p("IBAN", d_iban, s_iban, "MISMATCH",
                             f"differs from position {pos}", kind="iban"))
            crit("SAP-001", f"IBAN mismatch: document {_dv('iban', d_iban)} vs "
                            f"SAP {_dv('iban', s_iban)} (differs from position {pos}). "
                            "Do not process — request corrected form or banking support.")
    elif d_iban and iban_decomposed:
        rows.append(_row_p("IBAN", d_iban, "", "match",
                         "SAP stores the IBAN decomposed (bank key + account) — consistent",
                         kind="iban"))
    elif d_iban or s_iban:
        rows.append(_row_p("IBAN", d_iban, s_iban, "only-one-side", kind="iban"))
        warn("SAP-002", "IBAN present on only one side (document vs SAP).")

    # Account number — SAP pads with leading zeros
    d_acc, s_acc = _norm_id(f.get("account_number")), _norm_id(sap.get("bank_account"))
    if d_acc and s_acc:
        if d_acc == s_acc:
            rows.append(_row_p("Bank Account", d_acc, s_acc, "match", kind="account_number"))
        elif d_acc.lstrip("0") == s_acc.lstrip("0"):
            rows.append(_row_p("Bank Account", d_acc, s_acc, "match",
                             "leading zeros differ (SAP padding)", kind="account_number"))
        elif s_iban and d_acc and d_acc in s_iban:
            rows.append(_row_p("Bank Account", d_acc, s_acc, "match",
                             "document account contained in SAP IBAN", kind="account_number"))
        else:
            pos = _first_diff(d_acc.lstrip("0"), s_acc.lstrip("0"))
            rows.append(_row_p("Bank Account", d_acc, s_acc, "MISMATCH",
                             f"differs from position {pos}", kind="account_number"))
            crit("SAP-003", f"Account number mismatch: document {_dv('account_number', d_acc)} "
                            f"vs SAP {_dv('account_number', s_acc)}. Do not process.")
    elif s_acc and acc_in_doc_iban and not d_acc:
        # SAP has the account, the document carries only the IBAN — the IBAN's
        # account part IS the SAP account. Confirm rather than "only one side".
        rows.append(_row_p("Bank Account", "from IBAN", s_acc, "match",
                         "SAP stores the IBAN account part", kind="account_number"))
    elif d_acc or s_acc:
        rows.append(_row_p("Bank Account", d_acc, s_acc, "only-one-side", kind="account_number"))

    # SWIFT/BIC — 8-char == 11-char with XXX head-office suffix
    d_sw, s_sw = _norm_id(f.get("swift_bic")), _norm_id(sap.get("swift_bic"))
    if d_sw and s_sw:
        eq = d_sw == s_sw or d_sw + "XXX" == s_sw or d_sw == s_sw + "XXX"
        note = "XXX head-office suffix" if eq and d_sw != s_sw else ""
        rows.append(_row_p("SWIFT/BIC", d_sw, s_sw, "match" if eq else "MISMATCH", note))
        if not eq:
            crit("SAP-004", f"SWIFT/BIC mismatch: document {d_sw} vs SAP {s_sw}.")
    elif d_sw or s_sw:
        rows.append(_row_p("SWIFT/BIC", d_sw, s_sw, "only-one-side"))

    # Bank country
    d_c, s_c = to_iso2(str(f.get("bank_country") or "")), to_iso2(sap.get("bank_country", ""))
    if d_c and s_c:
        rows.append(_row_p("Bank Country", d_c, s_c, "match" if d_c == s_c else "MISMATCH"))
        if d_c != s_c:
            crit("SAP-005", f"Bank country mismatch: document {d_c} vs SAP {s_c}.")
    elif d_c or s_c:
        rows.append(_row_p("Bank Country", d_c, s_c, "only-one-side"))

    # Bank key — must be confirmed by DOCUMENT-side data: inside the document's
    # IBAN (after CC+check digits) or equal to the document's routing/sort code.
    # Containment in SAP's own IBAN proves nothing about the document.
    s_key = _norm_id(sap.get("bank_key"))
    if s_key:
        d_routing = _norm_id(f.get("routing_aba"))
        d_wires = _norm_id(f.get("routing_aba_wires"))
        # window = country(2)+check(2)=4, then the bank code; +2 tolerates the
        # IT CIN letter that sits between the check digits and the ABI/CAB code
        # (IT ABI+CAB is iban[5:15], not iban[4:14] — the old max(...,10) window
        # dropped the last CAB digit and flagged every correct Italian row).
        in_doc_iban = bool(d_iban and s_key in d_iban[4:4 + len(s_key) + 2])
        eq_routing = bool((d_routing and d_routing == s_key)
                          or (d_wires and d_wires == s_key))
        if in_doc_iban or eq_routing:
            status, note = "match", ("confirmed by document IBAN" if in_doc_iban
                                     else "matches document routing")
        elif d_routing or d_iban:
            status, note = "MISMATCH", "no document-side confirmation"
            warn("SAP-006", f"SAP Bank Key {s_key} is not confirmed by the document "
                            "(not in the IBAN bank-code position, routing differs).")
        else:
            status, note = "sap-only", "document shows no routing/IBAN to compare"
        doc_side = (_dv("routing_aba", d_routing or d_wires) if (d_routing or d_wires)
                    else ("from IBAN" if d_iban else ""))
        rows.append(_row_p("Bank Key", doc_side, s_key, status, note))

    # Bank name — normalized containment either way
    d_bn, s_bn = _norm_name(f.get("bank_name")), _norm_name(sap.get("bank_name"))
    if d_bn and s_bn:
        eq = d_bn in s_bn or s_bn in d_bn
        rows.append(_row_p("Bank Name", f.get("bank_name", ""), sap.get("bank_name", ""),
                         "match" if eq else "MISMATCH"))
        if not eq:
            warn("SAP-007", f"Bank name differs: document '{f.get('bank_name')}' vs "
                            f"SAP '{sap.get('bank_name')}'.")
    elif d_bn or s_bn:
        rows.append(_row_p("Bank Name", f.get("bank_name", ""), sap.get("bank_name", ""),
                         "only-one-side"))

    # Account holder vs SAP Account Holder / Account Name
    d_h = _norm_name(f.get("account_holder"))
    s_h = _norm_name(sap.get("account_holder") or sap.get("account_name"))
    if d_h and s_h:
        # containment OR materially-equal (legal-suffix/word-order tolerant:
        # 'ACME S.p.A.' vs 'ACME SpA', 'Snaco GmbH' vs 'Snaco')
        eq = (d_h in s_h or s_h in d_h
              or names_materially_equal(f.get("account_holder"),
                                        sap.get("account_holder") or sap.get("account_name")))
        rows.append(_row_p("Account Holder", f.get("account_holder", ""),
                         sap.get("account_holder") or sap.get("account_name", ""),
                         "match" if eq else "MISMATCH"))
        if not eq:
            crit("SAP-008", f"Account holder differs: document '{f.get('account_holder')}' "
                            f"vs SAP '{sap.get('account_holder') or sap.get('account_name')}'.")
    elif d_h or s_h:
        rows.append(_row_p("Account Holder", f.get("account_holder", ""),
                         sap.get("account_holder") or sap.get("account_name", ""),
                         "only-one-side"))

    # SAP-only informational fields
    for key, label in (("bank_details_id", "Bank Details ID"), ("control_key", "Control Key"),
                       ("reference_details", "Reference Details"), ("city", "City")):
        if sap.get(key):
            rows.append(_row_p(label, "", sap[key], "sap-only"))

    if not any(r["status"] == "MISMATCH" for r in rows) and rows:
        findings.append(Finding("SAP-000", "NOTE", None,
                                "SAP comparison: all comparable fields match."))
    return findings, rows
