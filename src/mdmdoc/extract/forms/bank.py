"""Bank document → fixed schema (holder / bank / country / IBAN / account /
routing / SWIFT / clearing code / currency).

Identifiers come from the extractor's consensus tokens (status already decided
there: confirmed / checksum_ok / review). The narrative fields — who holds the
account, which bank, which country, which currency — are read LABEL-ANCHORED
from every engine's transcript and voted the same way: two engine families
agreeing confirm a value, one reading alone is handed over for review.
"""
from __future__ import annotations

import re

from .common import Field, absent, anchored, find_line, looks_like_label, norm_text, vote

# label → value on the same line ("Account holder: ACME GmbH") or on the next
# non-empty line (a form laid out label-above-value). Labels per language.
LABELS = {
    "account_holder": re.compile(
        r"(?i)^\W*(?:account\s*holder(?:'s)?(?:\s*name)?|account\s*name|name\s*(?:of|on)\s*(?:the\s*)?"
        r"account(?:\s*holder)?|beneficiary(?:\s*name)?|benef\.?|holder|titulaire(?:\s*du\s*compte)?|"
        r"kontoinhaber|inhaber|beneficiario|titular|intestatario|intestato\s*a|razón\s*social|"
        r"cliente|customer\s*name|company\s*name|payee|户名|账户名称|客户名称|名義|口座名義(?:人)?|예금주)"
        r"\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "bank_name": re.compile(
        r"(?i)^\W*(?:bank\s*name|name\s*of\s*(?:the\s*)?bank|beneficiary\s*bank|bank|banque|banco|banca|"
        r"bankname|kreditinstitut|开户银行|开户行|銀行名|은행명|은행)\s*[:：\-–|]\s*(?P<v>.*)$"),
    "bank_country": re.compile(
        r"(?i)^\W*(?:bank\s*country|country\s*of\s*(?:the\s*)?bank|country|pays|país|paese|land)"
        r"\s*[:：\-–|]\s*(?P<v>.*)$"),
    "currency": re.compile(
        r"(?i)^\W*(?:currency|account\s*currency|devise|moneda|valuta|währung|币种|货币|通貨|통화)"
        r"\s*[:：\-–|]?\s*(?P<v>.*)$"),
}
_CUR = re.compile(r"\b(USD|EUR|GBP|CHF|JPY|CNY|RMB|KRW|PLN|HUF|CZK|SEK|NOK|DKK|CAD|AUD|BRL|MXN|CLP|COP|ARS|INR|TRY|ILS|AED|SAR|SGD|HKD|TWD|THB|ZAR)\b")
_BANK_WORD = re.compile(r"(?i)\b(\w{0,12}bank|banque|banco|banca|bancaria|sparkasse|raiffeisen|credit\s*union|銀行|은행|银行)\b")
_ADDRESS_NOISE = re.compile(r"(?i)\b(street|str\.|strasse|straße|avenue|ave\.|road|rd\.|p\.?o\.? box|suite|floor)\b|\d{4,}")


_anchored = anchored               # shared with the generic reader (forms/common.py)
_looks_like_label = looks_like_label


def _bank_name_fallback(readings: dict[str, str]) -> dict[str, str]:
    """No "Bank name:" label: the first short line naming a bank near the top
    (letterhead), skipping lines that read like an address."""
    out = {}
    for eid, text in (readings or {}).items():
        for ln in [l.strip() for l in (text or "").split("\n") if l.strip()][:12]:
            if _BANK_WORD.search(ln) and len(ln) <= 60 and not _ADDRESS_NOISE.search(ln):
                ln = re.sub(r"(?i)^\W*(?:bankdaten|bankverbindung|bank(?:\s*name)?)\s*[:：\-–|]?\s+", "", ln)
                out[eid] = ln.strip(" :：-–|")
                break
    return out


def _token_field(entry: dict, page_no: int) -> Field:
    v = entry.get("value") or ""
    core = v.split(":", 1)[1] if ":" in v else v
    return Field(value=core, pretty=entry.get("pretty") or core, status=entry.get("status") or "review",
                 page=page_no, bbox_pct=entry.get("bbox_pct"), evidence=entry.get("label") or "",
                 voices=list(entry.get("voices") or []))


_ORDER = {"confirmed": 0, "checksum_ok": 1, "review": 2}


def _pick(cands: list[Field]) -> Field:
    return min(cands, key=lambda f: (_ORDER.get(f.status, 3), f.page or 0)) if cands else absent()


def _best_status(cands: list[Field]) -> int:
    return min((_ORDER.get(f.status, 3) for f in cands), default=3)


def read(doc: dict) -> tuple[dict[str, dict], dict]:
    """→ (fields, extra). fields: schema key → Field.as_dict(); extra: candidates
    the host may want to show (several routings, several IBANs)."""
    pages = doc.get("pages_out") or []
    ibans, swifts, routings, wires, accounts, clearings = [], [], [], [], [], []
    for pg in pages:
        pno = int(pg.get("page", 0))
        for e in pg.get("fields") or []:
            kind = e.get("kind") or ""
            f = _token_field(e, pno)
            label = (e.get("label") or "").lower()
            if kind == "IBAN":
                ibans.append(f)
            elif kind == "BIC / SWIFT":
                swifts.append(f)
            elif kind == "routing (ABA)":
                (wires if "wire" in label else routings).append(f)
            elif kind == "account":
                accounts.append(f)
            elif kind == "bank code":
                f.evidence = label
                clearings.append(f)
    fields: dict[str, Field] = {
        "iban": _pick(ibans), "swift_bic": _pick(swifts), "routing_aba": _pick(routings),
        "routing_aba_wires": _pick(wires), "national_clearing": _pick(clearings),
    }
    # An account number that is only the tail of the IBAN is the same value
    # twice — unless dropping it would leave a WORSE reading behind: on the AMF
    # self-disclosure "321 167 800" (two engines) is the IBAN's tail while
    # tesseract's truncated "321167" is not, and dropping the good one handed
    # the host a mismatch against the form (2026-08-30).
    iban_digits = re.sub(r"\D", "", fields["iban"].value)
    tails = [a for a in accounts if iban_digits and re.sub(r"\D", "", a.value)
             and iban_digits.endswith(re.sub(r"\D", "", a.value))]
    rest = [a for a in accounts if a not in tails]
    if rest and tails and _best_status(rest) <= _best_status(tails):
        accounts = rest
    fields["account_number"] = _pick(accounts)
    if not fields["routing_aba"].value and fields["routing_aba_wires"].value:
        fields["routing_aba"] = fields["routing_aba_wires"]

    # narrative fields: label-anchored, voted across engines
    for key, rx in LABELS.items():
        cands: dict[str, str] = {}
        for pg in pages:
            if cands:
                break
            cands = _anchored(pg.get("readings") or {}, rx)
            if key == "bank_name" and not cands:
                cands = _bank_name_fallback(pg.get("readings") or {})
            if key == "currency":
                cands = {e: (m.group(1) if (m := _CUR.search(v.upper())) else "") for e, v in cands.items()}
                cands = {e: v for e, v in cands.items() if v}
            if cands:
                raw, status, voices = vote(cands)
                eid, bbox = find_line(pg, raw)
                fields[key] = Field(value=raw, pretty=raw, status=status, page=int(pg.get("page", 0)),
                                    bbox_pct=bbox, evidence=raw, voices=voices)
        fields.setdefault(key, absent())
    if not fields["currency"].value:
        # a currency token anywhere in the transcript, when the document names one only
        seen = set()
        for pg in pages:
            for text in (pg.get("readings") or {}).values():
                seen.update(m.group(1) for m in _CUR.finditer((text or "").upper()))
        if len(seen) == 1:
            fields["currency"] = Field(value=next(iter(seen)), status="review", evidence="currency token")
    if not fields["bank_country"].value and fields["iban"].value[:2].isalpha():
        fields["bank_country"] = Field(value=fields["iban"].value[:2].upper(), pretty=fields["iban"].value[:2].upper(),
                                       status=fields["iban"].status, page=fields["iban"].page,
                                       bbox_pct=fields["iban"].bbox_pct, evidence="IBAN country prefix",
                                       voices=fields["iban"].voices)
    extra = {
        "ibans": [f.as_dict() for f in ibans],
        "routing_candidates": [dict(f.as_dict(), source="document — ACH") for f in routings]
                              + [dict(f.as_dict(), source="document — wires") for f in wires],
        "accounts": [f.as_dict() for f in accounts],
    }
    return {k: v.as_dict() for k, v in fields.items()}, extra
