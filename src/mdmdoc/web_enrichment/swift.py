#!/usr/bin/env python3
"""
swift.py — SWIFT/BIC evidence (MVP item 2).

OFFLINE and deterministic (Tier 1 structural): validate BIC syntax and pull the
ISO-3166 country out of positions 5-6, cross-checking it against the document's
stated bank country. No network needed, so this works with web evidence enabled
but the machine offline.

FULL directory lookup (BIC -> institution legal name + address) is the SWIFTRef
service — a licensed/paid feed. It is a documented OPTIONAL connector: set
MDMDOC_SWIFT_LOOKUP_URL (an {bic} template returning {"name","address"}) to
enable it. Unset => we simply don't attempt it (the syntax/country hint stands).

Only the BIC and country leave the machine (and only if the paid connector is
configured) — never account/IBAN/TIN.
"""
from __future__ import annotations

import os
import re

from ..fields import to_iso2
from . import http
from .evidence import CONFLICT, FOUND, NOT_FOUND, UNAVAILABLE, Evidence

# BIC = 4 bank + 2 country + 2 location (+ optional 3 branch) => 8 or 11 chars.
BIC_RE = re.compile(r"^[A-Z]{4}([A-Z]{2})([A-Z0-9]{2})([A-Z0-9]{3})?$")

# ISO 3166-1 alpha-2 (offline country validity — a BIC can be from any country).
ISO_ALPHA2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM
BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX
CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG
GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR
IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV
LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE
NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF
TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF
WS YE YT ZA ZM ZW XK
""".split())


def _clean(bic: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(bic or "")).upper()


def parse_bic(bic: str) -> dict | None:
    """-> {bank, country, location, branch, primary, head_office} or None if invalid."""
    c = _clean(bic)
    m = BIC_RE.match(c)
    if not m:
        return None
    return {"bic": c, "bank": c[:4], "country": m.group(1), "location": m.group(2),
            "branch": m.group(3) or "", "primary": len(c) == 8,
            "head_office": (m.group(3) or "").upper() == "XXX"}


def _syntax_evidence(bic_raw: str, doc_country: str, vault) -> list[Evidence]:
    src = "SWIFT/BIC ISO-9362 syntax"
    parsed = parse_bic(bic_raw)
    c = _clean(bic_raw)
    if parsed is None:
        return [Evidence("swift_syntax", CONFLICT,
                        f"'{c}' is not a valid BIC (ISO-9362 is 8 or 11 chars: "
                        "4 bank + 2 country + 2 location [+3 branch])",
                        src, 1, "WEB-SWIFT-1", detail="malformed SWIFT/BIC",
                        query=f"bic:{c}")]
    out: list[Evidence] = []
    country = parsed["country"]
    if country not in ISO_ALPHA2:
        out.append(Evidence("swift_syntax", CONFLICT,
                            f"BIC {parsed['bic']} has an invalid country code '{country}'",
                            src, 1, "WEB-SWIFT-1", query=f"bic:{parsed['bic']}"))
        return out
    tail = " (head-office XXX)" if parsed["head_office"] else \
           (f" (branch {parsed['branch']})" if parsed["branch"] else "")
    out.append(Evidence("swift_syntax", FOUND,
                        f"BIC {parsed['bic']} is well-formed — country {country}{tail}",
                        src, 1, "WEB-SWIFT-1",
                        detail="structure/country valid; not proof the institution routes here",
                        query=f"bic:{parsed['bic']}"))
    # cross-check the BIC country against the document's stated bank country
    doc_iso = to_iso2(doc_country or "")
    if doc_iso and doc_iso != country:
        out.append(Evidence("swift_country", CONFLICT,
                            f"BIC country {country} != document bank country {doc_iso}",
                            "SWIFT/BIC vs document", 1, "WEB-SWIFT-2",
                            detail="the SWIFT code's country differs from the document — verify",
                            query=f"bic_country:{country};doc_country:{doc_iso}"))
    return out


def _paid_lookup_evidence(bic: str, vault) -> Evidence | None:
    """Optional licensed SWIFTRef-style directory lookup. Only when configured."""
    tmpl = os.environ.get("MDMDOC_SWIFT_LOOKUP_URL", "").strip()
    if not tmpl:
        return None
    src = "SWIFT directory (licensed connector)"
    url = tmpl.replace("{bic}", bic)
    data = http.get_json(url, vault=vault)
    if data is None or not isinstance(data, dict):
        return Evidence("swift_lookup", UNAVAILABLE,
                        f"BIC {bic}: directory connector unreachable",
                        src, 1, "WEB-SWIFT-3", tmpl, query=f"bic:{bic}")
    name = str(data.get("name") or "").strip()
    if not name:
        return Evidence("swift_lookup", NOT_FOUND, f"BIC {bic} not in the directory",
                        src, 1, "WEB-SWIFT-3", url, query=f"bic:{bic}")
    addr = str(data.get("address") or "").strip()
    return Evidence("swift_lookup", FOUND,
                    f"BIC {bic} = {name}" + (f", {addr}" if addr else ""),
                    src, 2, "WEB-SWIFT-3", url, query=f"bic:{bic}")


def collect(ext) -> list[Evidence]:
    if ext.doc_class != "bank":
        return []
    bic = _clean(ext.fields.get("swift_bic"))
    if not bic:
        return []
    out = _syntax_evidence(bic, str(ext.fields.get("bank_country") or ""), ext.vault)
    parsed = parse_bic(bic)
    if parsed is not None:
        paid = _paid_lookup_evidence(parsed["bic"], ext.vault)
        if paid is not None:
            out.append(paid)
    return out
