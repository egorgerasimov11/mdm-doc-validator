#!/usr/bin/env python3
"""
inventory.py — "Also on the document": label-anchored capture of every
labeled fact the extraction schema does not carry.

Why: the schema extracts what SAP needs, and everything else used to vanish
silently. Real cases: a Chinese bank letter carried a taxpayer ID, a phone,
an address and TWO account blocks the page never showed; a US remit form
carried FEIN/DUNS/two routings/a second SWIFT — half of it was dropped and
the rest came out mangled ("s Receivable") because v1 matched labels without
word boundaries and let its (?i) flag leak into the value patterns.

v2 engine (G-wave): matching is PER LINE; label alternatives are wrapped in
a scoped (?i:…) group so value classes stay case-sensitive; every latin label
is word-bounded so "Account" can never match inside "Accounts Receivable";
the separator window excludes newlines so a section header can never donate
its label to the next line. Unknown-but-labeled lines land in the generic
"other" family (curated singles + <…Number|Name|Code|…> shapes) — nothing is
dropped silently, and nothing is guessed from bare patterns either.

Pure and model-free: collect(text) returns FULL values (in-memory only);
stage_b._collect_inventory masks/registers them before anything persists.
"""
from __future__ import annotations

import re

# separator between a label and its value: NO newlines (v1 bug: \s let the
# label at the end of a header line capture the whole next line as a value)
_SEP = r"[ \t:：#.·|]{0,6}"

_SWIFT_SHAPE = r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?"
_SWIFT_QUALIFIERS = re.compile(
    r"(?i)\b(us domestic|domestic|foreign currency|international)\b")


def _lat(rx: str) -> str:
    """Latin word bounds around a label alternation (captured as group 1):
    'Account' must not match inside 'Accounts' or 'Accounting'."""
    return rf"(?<![A-Za-z])({rx})(?![A-Za-z])"


# family -> (label_regex, value_regex, sensitive_kind or "")
# Order = specificity: on one line several families may match (a CJK form line
# often carries 电话 and 传真 together); the generic "other" family runs only
# when NO named family matched the line.
_FAMILIES: tuple = (
    ("tax_id",
     _lat(r"(?i:纳税人识别号|统一社会信用代码|税号|FEIN(?:\s*(?:No|Number))?|EIN|"
          r"Federal\s+Tax\s+ID|Tax\s*ID(?:entification)?(?:\s*(?:No|Number))?|"
          r"VAT(?:\s*Reg(?:istration)?)?\s*(?:No|Number|ID)?|USt[-.]?\s?IdNr\.?|"
          r"P\.?\s?IVA|NIF|CIF|ИНН|Steuernummer)"),
     r"([A-Z0-9][A-Z0-9 \-/.]{5,24}[A-Z0-9])", "tin"),
    ("company_reg",
     _lat(r"(?i:营业执照(?:注册号)?|注册号|Company\s+Reg(?:istration)?\s*(?:No|Number)\.?|"
          r"D[- ]?U[- ]?N[- ]?S(?:\s*(?:No|Number))?|Handelsregister(?:nummer)?|HRB)"),
     r"([A-Z0-9][A-Z0-9 \-/.]{4,24}[A-Z0-9])", ""),
    ("holder",
     _lat(r"(?i:Name\s+on\s+(?:the\s+)?Account|Account\s+(?:Holder|Name)|"
          r"Beneficiary(?:\s+Name)?|户名|收款人(?:名称)?)"),
     r"(\S[^\n]{1,60})", ""),
    ("account",
     _lat(r"(?i:开户账号|收款账[户号]|银行账[户号]|账号|帐号|账户|口座番号|"
          r"Acc(?:oun)?t\s*(?:No|Number|#)\.?|IBAN|Cuenta|Konto(?:nummer)?)"),
     r"([0-9A-Z][0-9A-Z \-]{5,32}[0-9A-Z])", "account_number"),
    ("routing",
     _lat(r"(?i:(?:Bank\s+)?Routing(?:\s+(?:No|Number|#))?(?:\s+for)?"
          r"(?:\s+(?:ACH|Wires?|Domestic|International))?|ABA(?:\s*(?:No|Number|#))?|RTN)"),
     r"(\d[\d \-]{6,14}\d)", "routing_aba"),
    ("clearing",
     _lat(r"(?i:联行号码?|CNAPS|sort\s*code|BSB|IFSC|Bankleitzahl|BLZ)"),
     r"([0-9A-Z][0-9A-Z \-]{4,14}[0-9A-Z])", "routing_aba"),
    ("swift",
     _lat(r"(?i:SWIFT(?:\s*/?\s*BIC)?(?:\s*Code)?|BIC(?:\s*Code)?)"),
     rf"({_SWIFT_SHAPE})", ""),
    ("phone",
     _lat(r"(?i:电话|手机|传真|Tel(?:efon|éfono)?\.?|(?:Direct\s+(?:AR\s+)?|MAIN\s+)?Phone(?:\s+Number)?|"
          r"Fax(?:\s+Number)?|Mobile)"),
     r"([+()0-9][0-9 ()\-]{6,18}[0-9])", ""),
    ("city_state_zip",
     _lat(r"(?i:City,?\s*State,?\s*(?:&\s*)?Zip(?:\s*Code)?|City\s*/\s*State\s*/\s*Zip)"),
     r"(\S[^\n]{3,60})", ""),
    ("currency",
     _lat(r"(?i:Payment\s+Currency|Currency)"),
     r"([A-Z]{3})(?![A-Za-z])", ""),
    ("address",
     r"(?<![Mm][Aa][Ii][Ll] )" +   # "E-mail Address for …" is an email label, not an address
     _lat(r"(?i:(?:Mailing|Office|Bank(?:'s)?|Street|Registered|Company)\s+Address(?:\s+for)?|"
          r"Address|地址|Anschrift|Direcci[oó]n)"),
     r"(\S[^\n]{4,88})", ""),
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")

# generic fallback ("other"): a line whose leading words FORM a label —
# either a curated single word or a phrase ending in a label-noun.
_OTHER_SINGLE = re.compile(
    r"^[ \t]{0,3}(City|State|Country|Region|Province|Branch|Attn|Contact(?: Person)?)"
    rf"{_SEP}(\S[^\n]{{1,80}})")
_OTHER_GENERIC = re.compile(
    r"^[ \t]{0,3}([A-Z][A-Za-z'’/&().\- ]{1,38}?"
    r"(?:Number|No\.?|Name|Code|ID|Type|Terms|Date|Method|Submission|Administrator|Confirmation))"
    rf"{_SEP}(\S[^\n]{{1,80}})")

_DIGIT_RUN = re.compile(r"\d{7,}")
_MAX_ROWS, _MAX_OTHER = 40, 15


def _trim_phone(v: str) -> str:
    """Cut trailing bare digit groups once a full number is accumulated —
    'Tel: 858-500-5510 4225 Executive Square' must not swallow the house
    number into the phone."""
    keep, digits = [], 0
    for part in v.split(" "):
        if digits >= 10 and not re.search(r"[()\-]", part):
            break
        keep.append(part)
        digits += len(re.sub(r"\D", "", part))
    return " ".join(keep).strip()


def _norm(value: str) -> str:
    v = re.sub(r"[\s\-]", "", value)
    return v.casefold()


def _matches_extracted(value: str, extracted: dict | None) -> str:
    """Field name whose extracted value equals this inventory value (digit
    comparison for numeric identifiers, casefold otherwise) — the UI tags the
    row '= extracted' instead of presenting a schema duplicate as news."""
    if not extracted:
        return ""
    nv = _norm(value)
    dv = re.sub(r"\D", "", value)
    for field, fv in extracted.items():
        if not isinstance(fv, str) or not fv.strip():
            continue
        if len(dv) >= 6 and dv == re.sub(r"\D", "", fv):
            return field
        if nv and nv == _norm(fv):
            return field
    return ""


def _swift_rows(line: str, label: str, first: str) -> list[dict]:
    """All SWIFT codes on a labeled line, each annotated with the nearest
    qualifier ('US Domestic' / 'Foreign Currency' …) so the annotation the
    document PRINTS next to the code is not silently dropped."""
    rows = []
    for m in re.finditer(_SWIFT_SHAPE, line):
        code = m.group(0)
        window = line[max(0, m.start() - 8):m.end() + 30]
        q = _SWIFT_QUALIFIERS.search(window)
        base = label if code == first else "SWIFT (2nd)"
        tag = f"{base} ({q.group(1)})" if q else base
        rows.append({"family": "swift", "label": tag, "value": code, "kind": ""})
    return rows


def collect(text: str, extracted: dict | None = None) -> list[dict]:
    """-> [{family, label, value, kind, matches?}] with FULL values (in-memory
    only). Every labeled account occurrence is kept — duplicates across
    document blocks are SIGNAL (same value corroborates; different values are
    an operational risk). `extracted` (flat field->str) lets rows that repeat
    an extraction field carry a `matches` tag instead of posing as news."""
    out: list[dict] = []
    seen: set = set()

    def push(family: str, label: str, value: str, kind: str) -> None:
        label = re.sub(r"\s+", " ", label.strip(" :：#.·|-"))
        value = value.strip().strip("|").rstrip(".,;").strip()
        min_len = 2 if family in ("holder", "other", "city_state_zip", "phone",
                                  "currency") else 4
        if len(value) < min_len or len(label) > 42:
            return
        if family == "other" and not kind and _DIGIT_RUN.search(value):
            kind = "account_number"   # unknown labeled identifier with a long
            #                           digit run: mask/register like an account
        dedupe = (family, label.casefold(), _norm(value))
        if dedupe in seen and family != "account":
            return
        seen.add(dedupe)
        row = {"family": family, "label": label, "value": value, "kind": kind}
        m = _matches_extracted(value, extracted)
        if m:
            row["matches"] = m
        out.append(row)

    for line in (text or "").splitlines():
        if not line.strip():
            continue
        named = False
        for family, label_rx, value_rx, kind in _FAMILIES:
            for m in re.finditer(label_rx + _SEP + value_rx, line):
                named = True
                label, value = m.group(1), m.group(2)
                if family == "phone":
                    value = _trim_phone(value)
                if family == "swift":
                    out_rows = _swift_rows(line, re.sub(r"\s+", " ", label.strip()),
                                           value)
                    for r in out_rows:
                        push(r["family"], r["label"], r["value"], r["kind"])
                    break   # one swift pass covers the whole line
                push(family, label, value, kind)
        # bare e-mail addresses (any line): the label is whatever sits between
        # the previous e-mail and this one (never another address, never a
        # mid-word cut)
        prev_end = 0
        for m in _EMAIL_RE.finditer(line):
            named = True
            prefix = re.sub(r"\s+", " ", line[prev_end:m.start()]).strip(" :：#.·|-/")
            prev_end = m.end()
            if len(prefix) > 40:
                prefix = prefix[-40:]
                prefix = prefix.split(" ", 1)[1] if " " in prefix else prefix
            push("email", prefix or "E-mail", m.group(0), "")
        if named:
            continue
        om = _OTHER_SINGLE.match(line) or _OTHER_GENERIC.match(line)
        if om and sum(1 for r in out if r["family"] == "other") < _MAX_OTHER:
            push("other", om.group(1), om.group(2).lstrip("-–— ").strip(), "")

    return out[:_MAX_ROWS]
