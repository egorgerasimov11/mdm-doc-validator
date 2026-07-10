#!/usr/bin/env python3
"""
inventory.py — "Also on the document": label-anchored capture of every
identifier-bearing line the extraction schema does not carry.

Why: the schema extracts what SAP needs, and everything else used to vanish
silently. Real case: a Chinese bank letter carried a taxpayer ID
(纳税人识别号 …233T), a phone, an address and TWO bank-account blocks — the run
page showed none of it, and the tax ID was not even leak-gated (an 18-char
alnum CN TIN matches no SSN/EIN shape).

Pure and model-free: collect(text) returns FULL values (in-memory only);
stage_b._collect_inventory masks/registers them before anything persists.
Families are label-anchored — no bare-pattern guessing.
"""
from __future__ import annotations

import re

# family -> (label_regex, value_regex, sensitive_kind or "")
# The label regex must end where the value starts; the value regex is anchored
# right after the label (with a short separator window).
_SEP = r"[\s:：#.·]{0,6}"
_FAMILIES: tuple = (
    ("tax_id",
     r"(?i)(纳税人识别号|统一社会信用代码|税号|Tax\s*ID(?:\s*(?:No|Number))?|"
     r"VAT(?:\s*Reg(?:istration)?)?\s*(?:No|Number|ID)?|USt[-.]?\s?IdNr\.?|"
     r"P\.?\s?IVA|NIF|CIF|ИНН|Steuernummer)",
     r"([A-Z0-9][A-Z0-9 \-/.]{5,24}[A-Z0-9])", "tin"),
    ("company_reg",
     r"(?i)(营业执照(?:注册号)?|注册号|Company\s+Reg(?:istration)?\s*(?:No|Number)\.?|"
     r"Handelsregister(?:nummer)?|HRB)",
     r"([A-Z0-9][A-Z0-9 \-/.]{4,24}[A-Z0-9])", ""),
    ("account",
     r"(?i)(开户账号|收款账[户号]|银行账[户号]|账号|帐号|账户|口座番号|"
     r"acc(?:oun)?t\s*(?:no|number|#)?\.?|IBAN|cuenta|konto(?:nummer)?)",
     r"([0-9A-Z][0-9A-Z \-]{5,32}[0-9A-Z])", "account_number"),
    ("clearing",
     r"(?i)(联行号码?|CNAPS|sort\s*code|BSB|IFSC|Bankleitzahl|BLZ)",
     r"([0-9A-Z][0-9A-Z \-]{4,14}[0-9A-Z])", "routing_aba"),
    ("phone",
     r"(?i)(电话|手机|传真|Tel(?:efon|éfono)?\.?|Phone|Fax|Mobile)",
     r"([+()0-9][0-9 ()\-]{6,18}[0-9])", ""),
    ("email", r"(?i)(E-?mail|邮箱)", r"([\w.+-]+@[\w-]+\.[\w.]{2,})", ""),
    ("address",
     r"(?i)(地址|Address|Anschrift|Direcci[oó]n)",
     r"([^\n]{6,90})", ""),
)

_LABEL_ONLY = {"iban"}   # families whose LABEL may equal a schema field name


def collect(text: str) -> list[dict]:
    """-> [{family, label, value, kind}] with FULL values (in-memory only).
    Every labeled occurrence is kept — duplicates across document blocks are
    SIGNAL (two account blocks with the same value corroborate it; with
    different values they are an operational risk)."""
    t = text or ""
    out: list[dict] = []
    seen: set = set()
    for family, label_rx, value_rx, kind in _FAMILIES:
        for m in re.finditer(label_rx + _SEP + value_rx, t):
            label = re.sub(r"\s+", " ", m.group(1).strip(" :：#.·"))
            value = m.group(2).strip().rstrip(".,;")
            if family == "address":
                value = value.strip()
            if not value or len(value) < 4:
                continue
            key = (family, label.casefold(), re.sub(r"[\s\-]", "", value).casefold(),
                   m.start())
            dedupe = key[:3]
            if dedupe in seen and family != "account":
                continue   # accounts keep every occurrence (block counting)
            seen.add(dedupe)
            out.append({"family": family, "label": label, "value": value,
                        "kind": kind})
    return out
