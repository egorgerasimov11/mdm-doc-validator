#!/usr/bin/env python3
"""bulk.taxmath — deterministic tax-number check algorithms (pure functions).

Each checker takes the NORMALIZED value (uppercase, no spaces/dots/dashes,
national prefix stripped where noted) and returns (ok: bool, detail: str).
They are referenced BY NAME from rules/bulk_tax.yaml (`checksum:` key) — the
catalog stays data, the arithmetic lives here. Unknown name -> no checksum run.

Sources: ISO 7064 MOD 11,10 (DE UStIdNr), the published VIES national formats,
IRS SSN/EIN assignment rules, ATO ABN mod-89, Receita CNPJ/CPF, GB mod-97-55,
CN GB32100 USCC mod-31, GSTIN base-36 check.
"""
from __future__ import annotations

import re


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# --- US ---------------------------------------------------------------------
def us_ssn(v: str):
    d = _digits(v)
    if len(d) != 9:
        return False, f"{len(d)} digits, SSN must be 9"
    area, group, serial = d[:3], d[3:5], d[5:]
    if area == "000" or area == "666" or area >= "900":
        return False, f"area {area} is never assigned (000/666/9xx are invalid SSN areas)"
    if group == "00" or serial == "0000":
        return False, "group 00 / serial 0000 are never assigned"
    return True, ""


# EIN campus prefixes the IRS has never assigned
_EIN_BAD_PREFIX = {"00", "07", "08", "09", "17", "18", "19", "28", "29",
                   "49", "69", "70", "78", "79", "89", "96", "97"}


def us_ein(v: str):
    d = _digits(v)
    if len(d) != 9:
        return False, f"{len(d)} digits, EIN must be 9"
    if d[:2] in _EIN_BAD_PREFIX:
        return False, f"prefix {d[:2]} is not an IRS campus prefix"
    return True, ""


def us_itin(v: str):
    d = _digits(v)
    if len(d) != 9 or d[0] != "9":
        return False, "ITIN must be 9 digits starting with 9"
    return True, ""


# --- EU VAT ------------------------------------------------------------------
def de_vat(v: str):
    """ISO 7064 MOD 11,10 over the 9 digits (DE UStIdNr)."""
    d = _digits(v)
    if len(d) != 9:
        return False, f"{len(d)} digits, DE VAT needs 9 after 'DE'"
    if d[0] == "0":
        return False, "DE VAT never starts with 0"
    product = 10
    for c in d[:8]:
        s = (int(c) + product) % 10 or 10
        product = (2 * s) % 11
    check = (11 - product) % 10
    if check != int(d[8]):
        return False, f"check digit {d[8]} != computed {check} (ISO 7064 MOD 11,10)"
    return True, ""


def it_vat(v: str):
    """11 digits, Luhn-style: odd positions as-is, even doubled with 9-fold."""
    d = _digits(v)
    if len(d) != 11:
        return False, f"{len(d)} digits, IT VAT must be 11"
    total = 0
    for i, c in enumerate(d[:10]):
        n = int(c)
        if i % 2 == 0:
            total += n
        else:
            n *= 2
            total += n if n < 10 else n - 9
    check = (10 - total % 10) % 10
    if check != int(d[10]):
        return False, f"check digit {d[10]} != computed {check}"
    return True, ""


def fr_vat(v: str):
    """FRkk + 9-digit SIREN; numeric key kk = (12 + 3*(SIREN mod 97)) mod 97.
    Alphanumeric keys exist (old scheme) — shape-checked only."""
    s = re.sub(r"[^0-9A-Z]", "", (v or "").upper())
    if len(s) != 11:
        return False, f"{len(s)} chars, FR VAT is 2 key chars + 9-digit SIREN"
    key, siren = s[:2], s[2:]
    if not siren.isdigit():
        return False, "SIREN part must be 9 digits"
    if key.isdigit():
        expect = (12 + 3 * (int(siren) % 97)) % 97
        if int(key) != expect:
            return False, f"key {key} != computed {expect:02d}"
    return True, ""


def es_nif(v: str):
    """Spanish NIF/NIE/CIF families — letter checks where defined."""
    s = re.sub(r"[^0-9A-Z]", "", (v or "").upper())
    if len(s) != 9:
        return False, f"{len(s)} chars, ES tax id must be 9"
    letters = "TRWAGMYFPDXBNJZSQVHLCKE"
    if re.fullmatch(r"\d{8}[A-Z]", s):                       # NIF (person)
        if s[8] != letters[int(s[:8]) % 23]:
            return False, f"NIF letter {s[8]} != computed {letters[int(s[:8]) % 23]}"
        return True, ""
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", s):                  # NIE
        num = {"X": "0", "Y": "1", "Z": "2"}[s[0]] + s[1:8]
        if s[8] != letters[int(num) % 23]:
            return False, "NIE check letter mismatch"
        return True, ""
    if re.fullmatch(r"[A-HJNP-SUVW]\d{7}[0-9A-J]", s):       # CIF (entity)
        digits = s[1:8]
        even = sum(int(c) for c in digits[1::2])
        odd = 0
        for c in digits[0::2]:
            n = 2 * int(c)
            odd += n if n < 10 else n - 9
        check = (10 - (even + odd) % 10) % 10
        ctrl = s[8]
        if ctrl.isdigit():
            ok = int(ctrl) == check
        else:
            ok = ctrl == "JABCDEFGHI"[check]
        if not ok:
            return False, "CIF control character mismatch"
        return True, ""
    return False, "not a recognized NIF/NIE/CIF shape"


def nl_vat(v: str):
    """NL: 9 digits + 'B' + 2 digits. Legacy mod-11 on the 9 (new sole-trader
    ids fail mod-11 by design -> only a SUSPICIOUS-grade detail)."""
    s = re.sub(r"[^0-9B]", "", (v or "").upper())
    if not re.fullmatch(r"\d{9}B\d{2}", s):
        return False, "shape must be 9 digits + B + 2 digits"
    d = s[:9]
    total = sum(int(c) * w for c, w in zip(d, range(9, 1, -1)))
    if total % 11 != int(d[8]):
        return True, "legacy mod-11 does not hold (normal for post-2020 sole traders)"
    return True, ""


def pl_nip(v: str):
    d = _digits(v)
    if len(d) != 10:
        return False, f"{len(d)} digits, PL NIP must be 10"
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    check = sum(int(c) * w for c, w in zip(d[:9], weights)) % 11
    if check == 10 or check != int(d[9]):
        return False, "NIP mod-11 check failed"
    return True, ""


def be_vat(v: str):
    d = _digits(v)
    if len(d) == 9:
        d = "0" + d
    if len(d) != 10 or d[0] not in "01":
        return False, "BE VAT is 10 digits starting 0 or 1"
    if (97 - int(d[:8]) % 97) != int(d[8:]):
        return False, "BE mod-97 check failed"
    return True, ""


def at_vat(v: str):
    """ATU + 8: weighted sum with digit-fold, check = (96 - sum) mod 10."""
    s = re.sub(r"[^0-9U]", "", (v or "").upper())
    d = s[1:] if s.startswith("U") else s
    if len(d) != 8 or not d.isdigit():
        return False, "AT VAT is 'U' + 8 digits"
    total = 0
    for i, c in enumerate(d[:7]):
        n = int(c) * (2 if i % 2 else 1)
        total += n if n < 10 else n - 9
    check = (96 - total) % 10
    if check != int(d[7]):
        return False, f"check digit {d[7]} != computed {check}"
    return True, ""


def gb_vat(v: str):
    """GB 9 digits: mod-97 or mod-97-55; 12 digits = branch suffix."""
    d = _digits(v)
    if len(d) == 12:
        d = d[:9]
    if len(d) != 9:
        return False, f"{len(d)} digits, GB VAT must be 9 (or 12 with branch)"
    total = sum(int(c) * w for c, w in zip(d[:7], range(8, 1, -1)))
    rem = (total + int(d[7:])) % 97
    if rem == 0 or (rem + 55) % 97 == 0:
        return True, ""
    return False, "GB mod-97/mod-97-55 check failed"


def ch_uid(v: str):
    """CHE + 9 digits, last is EAN/mod-11 style check."""
    d = _digits(v)
    if len(d) != 9:
        return False, "CH UID is CHE + 9 digits"
    weights = (5, 4, 3, 2, 7, 6, 5, 4)
    rem = sum(int(c) * w for c, w in zip(d[:8], weights)) % 11
    check = (11 - rem) % 11
    if check == 10 or check != int(d[8]):
        return False, "UID mod-11 check failed"
    return True, ""


# --- non-EU ------------------------------------------------------------------
def au_abn(v: str):
    d = _digits(v)
    if len(d) != 11:
        return False, f"{len(d)} digits, ABN must be 11"
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    nums = [int(d[0]) - 1] + [int(c) for c in d[1:]]
    if sum(n * w for n, w in zip(nums, weights)) % 89 != 0:
        return False, "ABN mod-89 check failed"
    return True, ""


def br_cnpj(v: str):
    d = _digits(v)
    if len(d) != 14:
        return False, f"{len(d)} digits, CNPJ must be 14"

    def dv(base):
        weights = list(range(2, 10)) * 2
        s = sum(int(c) * w for c, w in zip(reversed(base), weights))
        r = s % 11
        return "0" if r < 2 else str(11 - r)

    if dv(d[:12]) != d[12] or dv(d[:13]) != d[13]:
        return False, "CNPJ check digits failed"
    return True, ""


def br_cpf(v: str):
    d = _digits(v)
    if len(d) != 11:
        return False, f"{len(d)} digits, CPF must be 11"
    if d == d[0] * 11:
        return False, "repeated-digit CPF is invalid"

    def dv(base, start):
        s = sum(int(c) * w for c, w in zip(base, range(start, 1, -1)))
        r = (s * 10) % 11
        return "0" if r == 10 else str(r)

    if dv(d[:9], 10) != d[9] or dv(d[:10], 11) != d[10]:
        return False, "CPF check digits failed"
    return True, ""


def cn_uscc(v: str):
    """CN Unified Social Credit Code: 18 chars, GB32100 mod-31 check."""
    s = re.sub(r"\s", "", (v or "").upper())
    alphabet = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    if len(s) != 18 or any(c not in alphabet for c in s):
        return False, "USCC must be 18 chars from the GB32100 alphabet (no I/O/S/V/Z)"
    weights = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
    total = sum(alphabet.index(c) * w for c, w in zip(s[:17], weights))
    check = (31 - total % 31) % 31
    if alphabet[check] != s[17]:
        return False, f"check char {s[17]} != computed {alphabet[check]}"
    return True, ""


def in_gstin(v: str):
    """IN GSTIN: 15 chars, base-36 double-weight check on position 15."""
    s = re.sub(r"\s", "", (v or "").upper())
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]", s):
        return False, "GSTIN shape is 99AAAAA9999A9Z9"
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for i, c in enumerate(s[:14]):
        n = alphabet.index(c) * (2 if i % 2 else 1)
        total += n // 36 + n % 36
    check = alphabet[(36 - total % 36) % 36]
    if check != s[14]:
        return False, f"check char {s[14]} != computed {check}"
    return True, ""


def luhn(v: str):
    d = _digits(v)
    if not d:
        return False, "no digits"
    total = 0
    for i, c in enumerate(reversed(d)):
        n = int(c)
        if i % 2 == 1:
            n *= 2
            n = n if n < 10 else n - 9
        total += n
    if total % 10 != 0:
        return False, "Luhn check failed"
    return True, ""


REGISTRY = {
    "us_ssn": us_ssn, "us_ein": us_ein, "us_itin": us_itin,
    "de_vat": de_vat, "it_vat": it_vat, "fr_vat": fr_vat, "es_nif": es_nif,
    "nl_vat": nl_vat, "pl_nip": pl_nip, "be_vat": be_vat, "at_vat": at_vat,
    "gb_vat": gb_vat, "ch_uid": ch_uid,
    "au_abn": au_abn, "br_cnpj": br_cnpj, "br_cpf": br_cpf,
    "cn_uscc": cn_uscc, "in_gstin": in_gstin, "luhn": luhn,
}
