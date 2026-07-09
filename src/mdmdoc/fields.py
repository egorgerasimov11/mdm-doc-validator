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

BANK_DOC_TYPES = ["bank_letter", "bank_statement", "supplier_letterhead", "bank_screenshot",
                  "voided_check", "payment_instructions", "ap_document", "invoice", "email",
                  "editable_source", "other"]
W9_DOC_TYPES = ["w9", "w8", "other_tax", "unknown"]

BANK_KEYS = ["account_holder", "account_type", "bank_name", "bank_country",
             "bank_address", "iban", "swift_bic", "account_number", "routing_aba",
             "routing_aba_wires", "branch_code", "currency", "doc_date", "signed",
             "signature_evidence", "partial_capture"]
W9_KEYS = ["line1_name", "line2_business_name", "line3_classification", "tin_type",
           "tin_raw", "address_street", "address_city_state_zip", "signed", "sign_date"]

ID_FIELDS = ("iban", "swift_bic", "account_number", "routing_aba", "routing_aba_wires")

_SENSITIVE_BANK = ("iban", "account_number", "routing_aba", "routing_aba_wires")
_SENSITIVE_W9 = ("tin_raw",)


def _norm_id(s) -> str:
    return re.sub(r"[\s\-.]", "", str(s or "")).upper()


# one box digit per line; a group dash may share the line ("1  – ") or own one
_BOXED_TIN_RE = re.compile(
    r"(?m)(?:^[ \t]*(?:[–\-—][ \t]*)?(?:\d[ \t]*(?:[–\-—][ \t]*)?|[–\-—][ \t]*)$\n?){9,13}")


def find_boxed_tin(text: str) -> tuple[str, str]:
    """W-9 digit boxes flatten to single-digit LINES (sometimes with a dash line
    between the EIN's 2-7 groups) — invisible to normal SSN/EIN regex.
    Returns (9 digits joined, 'EIN'|'SSN'|'') — the type is settled by the
    nearest PRECEDING box label, deterministic evidence the model can't override."""
    t = text or ""
    for m in _BOXED_TIN_RE.finditer(t):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) != 9:
            continue
        before = t[max(0, m.start() - 800):m.start()].lower()
        ei = before.rfind("employer identification")
        ss = before.rfind("social security")
        ttype = "EIN" if ei > ss else ("SSN" if ss > ei else "")
        return digits, ttype
    return "", ""


def _norm_name(s) -> str:
    t = re.sub(r"[^\w\s]", " ", str(s or ""), flags=re.UNICODE).casefold()
    return re.sub(r"\s+", " ", t).strip()


def crosscheck_ids(fields: dict, det: dict, doc_class: str = "bank",
                   policy: str = "masked", prov: dict | None = None) -> list[str]:
    """Deterministically verify model-read banking IDs against regex-extracted IDs.
    Fills blanks from OCR, confirms matches, flags mismatches. Banking values in
    notes follow the display policy; TIN branches are hard-masked regardless.
    Scoped by doc class: tax forms only cross-check the TIN — a stray digit run on
    a W-9 must not become an 'account number'.
    prov (optional) collects per-field provenance: OCR fills are recorded as
    source 'ocr-regex', model reads that OCR confirms get confirmed=True."""
    from .privacy import display_value
    notes = []
    prov = prov if prov is not None else {}
    for k in ID_FIELDS if doc_class == "bank" else ():
        dv = det.get(k)
        if not dv:
            continue
        kind = FIELD_KIND.get(k, "account_number")
        mv = fields.get(k, "")
        if not mv:
            fields[k] = dv
            notes.append(f"{k}=filled-from-OCR({display_value(kind, dv, policy)})")
            prov[k] = {"source": "ocr-regex", "page": None}
        elif _norm_id(mv) == _norm_id(dv):
            notes.append(f"{k}=confirmed")
            if prov.get(k, {}).get("source") != "ocr-regex":
                prov[k] = {"source": "model", "page": None, "confirmed": True}
        elif k == "account_number" \
                and _norm_id(mv).lstrip("0") == _norm_id(dv).lstrip("0") != "":
            # printed account vs the zero-padded IBAN part — same account
            notes.append(f"{k}=confirmed (zero-padded variant)")
            if prov.get(k, {}).get("source") != "ocr-regex":
                prov[k] = {"source": "model", "page": None, "confirmed": True}
        else:
            notes.append(f"{k}=MISMATCH(model={display_value(kind, mv, policy)} "
                         f"vs ocr={display_value(kind, dv, policy)})")
    # EIN found by OCR regex backs an unread TIN. The detector is EIN-specific
    # (label/format anchored), so it also SETTLES tin_type — deterministic
    # evidence beats a model guess.
    if doc_class == "w9" and det.get("ein"):
        if not str(fields.get("tin_raw") or "").strip():
            fields["tin_raw"] = det["ein"]
            notes.append(f"tin=filled-from-OCR({mask('ein', det['ein'])})")
            prov["tin_raw"] = {"source": "ocr-regex", "page": None}
        if _norm_id(fields.get("tin_raw")) == _norm_id(det["ein"]):
            if str(fields.get("tin_type") or "").upper() != "EIN":
                notes.append("tin_type=EIN (settled by the EIN detector)")
            fields["tin_type"] = "EIN"
    # W-9 digit boxes (one digit per line in the text layer)
    if doc_class == "w9" and det.get("tin_boxed"):
        if not str(fields.get("tin_raw") or "").strip():
            fields["tin_raw"] = det["tin_boxed"]
            notes.append(f"tin=filled-from-boxed-digits({mask('tin', det['tin_boxed'])})")
            prov["tin_raw"] = {"source": "ocr-regex", "page": None}
        boxed_type = str(det.get("tin_boxed_type") or "")
        if boxed_type and _norm_id(fields.get("tin_raw")) == _norm_id(det["tin_boxed"]):
            if str(fields.get("tin_type") or "").upper() != boxed_type:
                notes.append(f"tin_type={boxed_type} (settled by the {boxed_type} box label)")
            fields["tin_type"] = boxed_type
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


# --- page relevance scoring (multi-page docs: find WHERE the data lives) ------
_BANK_PAGE_KWS = ("iban", "swift", "bic", "account", "acct", "routing", "aba",
                  "sort code", "bank", "cuenta", "banco", "beneficiario",
                  "konto", "bankverbindung", "kontonummer", "blz", "empfänger",
                  "compte", "banque", "conta", "agência", "beneficiário",
                  "счет", "счёт", "банк", "реквизит", "получатель",
                  "계좌", "은행", "口座", "銀行", "账号", "帐号", "账户", "银行", "开户", "開戶")
_W9_PAGE_KWS = ("w-9", "taxpayer", "tin", "social security", "employer identification",
                "classification", "certification", "signature of", "disregarded")


_BANK_LETTER_PHRASES = ("please accept this letter", "account confirmation",
                        "confirmation that", "we confirm that", "maintained at",
                        "routing instructions", "letter as confirmation",
                        "bank confirmation", "treasury analyst", "to whom it may concern",
                        "this letter is to confirm", "account confirmation certificate",
                        # IT / ES / DE bank-confirmation wording
                        "conferma coordinate bancarie", "coordinate bancarie",
                        "vi confermiamo", "certificación bancaria",
                        "certificacion bancaria", "certificado bancario",
                        "bankbestätigung", "kontobestätigung")
_INVOICE_HEADER_RE = re.compile(r"(?im)^\s*(invoice|rechnung|fattura|factura)\s*$")
_INVOICE_MARKS = ("invoice number", "invoice no", "invoice date", "amount due",
                  "subtotal", "payment terms", "purchase order number", "pro forma",
                  "rechnungsnummer", "rechnungsdatum", "numero fattura",
                  "número de factura")
# "invoice ..." inside remittance/payment instructions describes the payer's
# FUTURE invoices, not this document — such lines are not invoice evidence
_REMIT_CONTEXT = ("remit", "being paid", "must include", "must be accompanied",
                  "when making payment", "payments received without", "amt:",
                  "payment amount per invoice", "ach payment", "payment instruction",
                  "wire transfer", "include the following")


def iban_mod97_ok(iban: str) -> bool:
    """ISO 13616 IBAN checksum. Explicit audit fact — reports state it out loud
    instead of leaving auditors to trust silence."""
    s = re.sub(r"\s", "", str(iban or "")).upper()
    if not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]{8,30}$", s):
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


def find_valid_ibans(text: str) -> list[str]:
    """IBAN candidates from raw text: spaces/full-width tolerated, mod-97-valid
    only. The deterministic source for repairing a model-garbled IBAN read."""
    import unicodedata
    t = unicodedata.normalize("NFKC", (text or "")).upper()
    out: list[str] = []
    for m in re.finditer(r"\b([A-Z]{2}\s?\d{2}(?:\s?[A-Z0-9]){10,32})\b", t):
        cand = re.sub(r"\s", "", m.group(1))
        if iban_mod97_ok(cand) and cand not in out:
            out.append(cand)
    return out


def looks_like_printed_email(text: str) -> bool:
    """A PDF that is a PRINTED EMAIL thread (Outlook-style headers) is email
    evidence even when a bank certificate is embedded as a screenshot."""
    head = (text or "")[:2000].lower()
    hits = sum(bool(re.search(rf"(?m)^\s*{h}:\s?\S", head))
               for h in ("from", "sent", "to", "subject"))
    return hits >= 3


def invoice_marks(text: str) -> int:
    """Structural invoice evidence: count invoice marks on lines OUTSIDE
    remittance-instruction context. A document is an invoice because it HAS an
    invoice number/date/amount of its own, not because it mentions the word."""
    n = 0
    for line in (text or "").lower().splitlines():
        if any(c in line for c in _REMIT_CONTEXT):
            continue
        n += sum(1 for m in _INVOICE_MARKS if m in line)
    return n


def page_markers(text: str) -> dict:
    """Per-page evidence: does this page look like a bank confirmation letter /
    an invoice? A packet is classified by its STRONGEST banking evidence — an
    invoice page elsewhere must not poison a packet that contains a bank letter."""
    t = (text or "").lower()
    letter_hits = sum(1 for p in _BANK_LETTER_PHRASES if p in t)
    bank_letter = letter_hits >= 2 or "account confirmation" in t \
        or "please accept this letter" in t or "this letter is to confirm" in t
    invoice = bool(_INVOICE_HEADER_RE.search(text or "")) or invoice_marks(text) >= 2
    return {"bank_letter": bank_letter, "invoice": invoice}


def page_score(text: str, doc_class: str) -> int:
    """How likely a page holds the data we need. Keyword hits + deterministic
    regex hits (weighted) + W-9 boxed-TIN bonus + bank-letter page bonus (the
    confirmation letter must outrank an invoice template's remittance footer)."""
    t = (text or "").lower()
    if not t.strip():
        return 0
    kws = _BANK_PAGE_KWS if doc_class == "bank" else _W9_PAGE_KWS
    score = sum(1 for k in kws if k in t)
    from . import ocr
    score += 3 * len(ocr.regex_fields(text))
    if doc_class == "w9" and find_boxed_tin(text)[0]:
        score += 5
    if doc_class == "bank":
        m = page_markers(text)
        if m["bank_letter"]:
            score += 8
        if m["invoice"] and not m["bank_letter"]:
            score -= 2
    return score


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
    # banking — a printed-out email thread is email evidence no matter what is
    # embedded in it; the TEXT-based invoice hint needs STRUCTURAL evidence (its
    # own invoice number/date/amount outside remittance context) and must not
    # fire when the packet also contains a bank confirmation letter page
    if looks_like_printed_email(text):
        return "email"
    if "invoice" in f or "rechnung" in f or "fattura" in f:
        return "invoice"
    if (bool(_INVOICE_HEADER_RE.search(text or "")) or invoice_marks(text) >= 2) \
            and not page_markers(t)["bank_letter"]:
        return "invoice"
    if has("voided check", "void check"):
        return "voided_check"
    if has("account statement", "e-statement", "estatement", "statement period",
           "statement of account", "kontoauszug", "extracto bancario",
           "estado de cuenta"):
        return "bank_statement"
    if has("certificación bancaria", "certificacion bancaria", "bankbestätigung",
           "bank confirmation", "bank letter", "开户许可证", "开户银行"):
        return "bank_letter"
    if has("bank account information") and has("docusign", "for sap", "s/4hana"):
        return "ap_document"
    if has("payment instruction", "ach payments only", "for ach payments",
           "remittance advice", "remittance sent", "email remittance",
           "banking details are confidential", "ach payment type"):
        return "payment_instructions"
    return ""


# --- extraction result ---------------------------------------------------------
@dataclass
class Extraction:
    doc_class: str                      # "bank" | "w9"
    doc_type: str = ""
    fields: dict = field(default_factory=dict)      # FULL values — in-memory only
    crosscheck: list = field(default_factory=list)  # policy-aware notes
    # field -> {source: model|ocr-regex|vision-crop|zone-probe|rule|precedent,
    #           page: 1-based page number or None, confirmed?: bool} — no values
    provenance: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    model_id: str = ""
    json_valid_first_try: bool = True
    tier: str = "fast"                  # "fast" | "strong" (escalated)
    escalated_because: list = field(default_factory=list)
    model_strong: str = ""
    strong_json_valid: bool = True
    signature_probe: dict = field(default_factory=dict)   # vision verdict on signature
    engine: str = "auto"                # effective analysis engine for this run
    # dual mode: [{field, deterministic(masked), llm(masked), agree, only}]
    engine_compare: list = field(default_factory=list)
    vault: SecretVault = field(default_factory=SecretVault)

    def register_secrets(self) -> None:
        keys = _SENSITIVE_BANK if self.doc_class == "bank" else _SENSITIVE_W9
        for k in keys:
            v = str(self.fields.get(k) or "")
            if v:
                self.vault.register(FIELD_KIND.get(k, "account_number"), v)

    def to_public(self, policy: str = "masked") -> dict:
        """Persistable view. policy='full' additionally exposes BANKING values in a
        'value' key (operator display policy); TIN never gets a value key —
        masked+derived facts only, under every policy."""
        pub: dict = {}
        for k, v in self.fields.items():
            sv = str(v or "").strip() if not isinstance(v, bool) else v
            if k == "iban" and sv:
                c = _norm_id(sv)
                pub[k] = {"masked": mask("iban", sv), "country": c[:2] if c[:2].isalpha() else "",
                          "length": len(c), "present": True}
                if policy == "full":
                    pub[k]["value"] = c
            elif k in ("account_number", "routing_aba", "routing_aba_wires") and sv:
                pub[k] = {"masked": mask(FIELD_KIND[k], sv), "length": len(_norm_id(sv)),
                          "present": True}
                if policy == "full":
                    pub[k]["value"] = _norm_id(sv)
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
        # provenance keyed by the PUBLIC field names (tin_raw folds into tin)
        prov = {("tin" if k == "tin_raw" else k): dict(v)
                for k, v in self.provenance.items() if k != "tin_type"}
        pub_wrap = {
            "doc_class": self.doc_class,
            "doc_type": self.doc_type,
            "fields": pub,
            "provenance": prov,
            "crosscheck": self.crosscheck,
            "warnings": self.warnings,
            "model": self.model_id,
            "json_valid_first_try": self.json_valid_first_try,
            "tier": self.tier,
            "escalated_because": self.escalated_because,
            "model_strong": self.model_strong,
            "signature_probe": self.signature_probe,
            "engine": self.engine,
            "sensitive_present": {it["kind"]: True for it in self.vault.items()},
        }
        if self.engine_compare:
            pub_wrap["engine_compare"] = self.engine_compare
        return pub_wrap
