#!/usr/bin/env python3
"""
fields.py — extraction contracts: field keys, doc-type taxonomy, pre-classifier
hints, deterministic ID cross-check and normalizers.
(Adapted from form-validator agents/extractor.py + packetlib.py, de-packetized.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .privacy import FIELD_KIND, SecretVault, mask

BANK_DOC_TYPES = ["bank_letter", "supplier_letterhead", "bank_screenshot", "voided_check",
                  "ap_document", "invoice", "email", "editable_source", "other"]
W9_DOC_TYPES = ["w9", "w8", "other_tax", "unknown"]

BANK_KEYS = ["account_holder", "bank_name", "bank_country", "bank_address", "iban",
             "swift_bic", "account_number", "routing_aba", "currency", "doc_date",
             "signed", "partial_capture"]
W9_KEYS = ["line1_name", "line2_business_name", "line3_classification", "tin_type",
           "tin_raw", "address_street", "address_city_state_zip", "signed", "sign_date"]

ID_FIELDS = ("iban", "swift_bic", "account_number", "routing_aba")

_SENSITIVE_BANK = ("iban", "account_number", "routing_aba")
_SENSITIVE_W9 = ("tin_raw",)


def _norm_id(s) -> str:
    return re.sub(r"[\s\-.]", "", str(s or "")).upper()


_BOXED_TIN_RE = re.compile(r"(?m)(?:^[ \t]*\d[ \t]*\n){8}^[ \t]*\d[ \t]*$")


def find_boxed_tin(text: str) -> str:
    """W-9 digit boxes flatten to 9 single-digit LINES in the PDF text layer —
    invisible to normal SSN/EIN regex. Returns the 9 digits joined, or ''."""
    m = _BOXED_TIN_RE.search(text or "")
    return re.sub(r"\s", "", m.group(0)) if m else ""


def _norm_name(s) -> str:
    t = re.sub(r"[^\w\s]", " ", str(s or ""), flags=re.UNICODE).casefold()
    return re.sub(r"\s+", " ", t).strip()


def crosscheck_ids(fields: dict, det: dict, doc_class: str = "bank") -> list[str]:
    """Deterministically verify model-read banking IDs against regex-extracted IDs.
    Fills blanks from OCR, confirms matches, flags mismatches. Masked notes only.
    Scoped by doc class: tax forms only cross-check the TIN — a stray digit run on
    a W-9 must not become an 'account number'."""
    notes = []
    for k in ID_FIELDS if doc_class == "bank" else ():
        dv = det.get(k)
        if not dv:
            continue
        kind = FIELD_KIND.get(k, "account_number")
        mv = fields.get(k, "")
        if not mv:
            fields[k] = dv
            notes.append(f"{k}=filled-from-OCR({mask(kind, dv)})")
        elif _norm_id(mv) == _norm_id(dv):
            notes.append(f"{k}=confirmed")
        else:
            notes.append(f"{k}=MISMATCH(model={mask(kind, mv)} vs ocr={mask(kind, dv)})")
    # EIN found by OCR regex backs an unread TIN
    if doc_class == "w9" and det.get("ein") and not fields.get("tin_raw"):
        fields["tin_raw"] = det["ein"]
        fields.setdefault("tin_type", "EIN")
        notes.append(f"tin=filled-from-OCR({mask('ein', det['ein'])})")
    # W-9 digit boxes (one digit per line in the text layer)
    if doc_class == "w9" and det.get("tin_boxed") and not str(fields.get("tin_raw") or "").strip():
        fields["tin_raw"] = det["tin_boxed"]
        notes.append(f"tin=filled-from-boxed-digits({mask('tin', det['tin_boxed'])})")
    return notes


# --- country normalization ---------------------------------------------------
COUNTRY_NAME_TO_ISO = {
    "brazil": "BR", "brasil": "BR", "chile": "CL", "colombia": "CO", "canada": "CA",
    "united states": "US", "usa": "US", "united states of america": "US", "u.s.": "US",
    "germany": "DE", "deutschland": "DE", "italy": "IT", "italia": "IT", "france": "FR",
    "netherlands": "NL", "nederland": "NL", "the netherlands": "NL", "holland": "NL",
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "japan": "JP", "korea": "KR",
    "south korea": "KR", "republic of korea": "KR", "china": "CN", "spain": "ES", "españa": "ES",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "austria": "AT", "poland": "PL",
    "belgium": "BE", "finland": "FI", "norway": "NO", "sweden": "SE", "ireland": "IE",
    "portugal": "PT", "denmark": "DK", "singapore": "SG", "hong kong": "HK", "malaysia": "MY",
    "united arab emirates": "AE", "uae": "AE", "mexico": "MX", "argentina": "AR", "india": "IN",
}
ISO2_SET = set(COUNTRY_NAME_TO_ISO.values())

_ISO3_TO_ISO2 = {"COL": "CO", "USA": "US", "DEU": "DE", "CHN": "CN", "GBR": "GB",
                 "ESP": "ES", "MEX": "MX", "BRA": "BR", "CHL": "CL", "CAN": "CA",
                 "FRA": "FR", "ITA": "IT", "NLD": "NL", "CHE": "CH", "JPN": "JP",
                 "KOR": "KR", "SGP": "SG", "ARE": "AE", "AUT": "AT", "PRT": "PT"}


def to_iso2(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    low = v.lower()
    if low in COUNTRY_NAME_TO_ISO:
        return COUNTRY_NAME_TO_ISO[low]
    up = v.upper()
    if len(up) == 2 and up in ISO2_SET:
        return up
    if up in _ISO3_TO_ISO2:
        return _ISO3_TO_ISO2[up]
    if len(up) > 2 and up[:2] in ISO2_SET and not up[2].isalpha():
        return up[:2]
    return ""


# --- W-9 classification normalization -----------------------------------------
def norm_classification(value: str) -> str:
    v = (value or "").lower()
    if "individual" in v or "sole" in v:
        return "individual_sole_prop"
    if "llc" in v or "limited liability" in v:
        return "llc"
    if "partnership" in v:
        return "partnership"
    if "corporation" in v or v.strip() in ("c corp", "s corp"):
        return "corporation"
    if "trust" in v or "estate" in v:
        return "trust_estate"
    if v.strip():
        return "other"
    return ""


_BUSINESS_SUFFIX_RE = re.compile(
    r"(?i)\b(llc|l\.l\.c|inc|incorporated|corp|corporation|ltd|limited|gmbh|s\.?a\.?s?|"
    r"pllc|llp|company|co\.)\b")


def looks_like_business(name: str) -> bool:
    return bool(_BUSINESS_SUFFIX_RE.search(name or ""))


# --- heuristic doc-type hint (deterministic; model refines within taxonomy) ----
def type_hint(filename: str, text: str, ext: str, doc_class: str) -> str:
    from . import config
    if ext in config.EDITABLE_EXTS:
        return "editable_source"
    if ext in config.EMAIL_EXTS:
        return "email"
    f = (filename or "").lower()
    t = (text or "").lower()
    blob = f + " " + t[:4000]

    def has(*kw):
        return any(k in blob for k in kw)

    if doc_class == "w9":
        if has("w-9", "w9", "request for taxpayer identification"):
            return "w9"
        if has("w-8", "w8ben", "w-8ben", "w8-ben", "certificate of foreign status"):
            return "w8"
        return ""
    # banking
    if "invoice" in f or has("pro forma invoice", "proforma invoice", "invoice no", "invoice number",
                             "rechnung", "fattura", "factura comercial"):
        return "invoice"
    if has("voided check", "void check"):
        return "voided_check"
    if has("certificación bancaria", "certificacion bancaria", "bankbestätigung",
           "bank confirmation", "bank letter", "开户许可证", "开户银行"):
        return "bank_letter"
    return ""


# --- extraction result ---------------------------------------------------------
@dataclass
class Extraction:
    doc_class: str                      # "bank" | "w9"
    doc_type: str = ""
    fields: dict = field(default_factory=dict)      # FULL values — in-memory only
    crosscheck: list = field(default_factory=list)  # masked notes
    warnings: list = field(default_factory=list)
    model_id: str = ""
    json_valid_first_try: bool = True
    vault: SecretVault = field(default_factory=SecretVault)

    def register_secrets(self) -> None:
        keys = _SENSITIVE_BANK if self.doc_class == "bank" else _SENSITIVE_W9
        for k in keys:
            v = str(self.fields.get(k) or "")
            if v:
                self.vault.register(FIELD_KIND.get(k, "account_number"), v)

    def to_public(self) -> dict:
        """Masked, persistable view: sensitive values replaced by masked + derived facts."""
        pub: dict = {}
        for k, v in self.fields.items():
            sv = str(v or "").strip() if not isinstance(v, bool) else v
            if k == "iban" and sv:
                c = _norm_id(sv)
                pub[k] = {"masked": mask("iban", sv), "country": c[:2] if c[:2].isalpha() else "",
                          "length": len(c), "present": True}
            elif k in ("account_number", "routing_aba") and sv:
                pub[k] = {"masked": mask(FIELD_KIND[k], sv), "length": len(_norm_id(sv)),
                          "present": True}
            elif k == "tin_raw":
                if sv:
                    digits = re.sub(r"\D", "", sv)
                    pub["tin"] = {"type": (self.fields.get("tin_type") or "").upper() or "unknown",
                                  "masked": mask("tin", sv), "digits": len(digits),
                                  "hyphenated": "-" in sv, "present": True}
                else:
                    pub["tin"] = {"type": (self.fields.get("tin_type") or "").upper() or "unknown",
                                  "masked": "", "digits": 0, "hyphenated": False, "present": False}
            elif k == "tin_type":
                continue  # folded into tin above
            else:
                pub[k] = v
        pub_wrap = {
            "doc_class": self.doc_class,
            "doc_type": self.doc_type,
            "fields": pub,
            "crosscheck": self.crosscheck,
            "warnings": self.warnings,
            "model": self.model_id,
            "json_valid_first_try": self.json_valid_first_try,
            "sensitive_present": {it["kind"]: True for it in self.vault.items()},
        }
        return pub_wrap
