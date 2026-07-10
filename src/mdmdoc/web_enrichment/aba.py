#!/usr/bin/env python3
"""
aba.py — US bank-routing evidence (MVP item 1).

Three layers, cheapest first:
  1. ABA checksum          OFFLINE, deterministic (Tier 1 structural). Always runs.
  2. FDIC BankFind API     Tier 1 registry: does a bank by this NAME exist and is
                           it ACTIVE? (banks.data.fdic.gov — free, official, no key)
  3. Fed E-Payments        Tier 1 registry: routing number -> owning bank name, so
     Routing Directory     we can cross-check "name matches routing owner". The
                           FRB directory has no stable free JSON API, so this is a
                           configurable connector (MDMDOC_FED_ROUTING_URL with an
                           {aba} placeholder returning {"name","active"}); unset =>
                           reported 'unavailable', never a failure.

Only routing numbers and bank NAMES leave the machine — never account/IBAN/TIN.
Every result is a NOTE-tier hint (see evidence.Evidence.to_finding).
"""
from __future__ import annotations

import os
import re

from . import http
from .evidence import CONFLICT, FOUND, NOT_FOUND, UNAVAILABLE, Evidence
from .match import best_match, name_matches as _name_matches

FDIC_URL = "https://banks.data.fdic.gov/api/institutions"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


# Single source of truth for the routing arithmetic — shared with the
# verdict-driving rule predicates (BNK-040..046). Re-exported under the old
# name so existing callers/tests keep working.
from ..rules.bankmath import checksum_valid  # noqa: E402


def _fdic_lookup(bank_name: str, vault) -> tuple[list[dict], bool]:
    """(matches, reachable). Query FDIC BankFind by NAME. Bank name only."""
    data = http.get_json(FDIC_URL, params={
        "search": f"NAME:{bank_name}",
        "fields": "NAME,CITY,STALP,ACTIVE,CERT",
        "limit": "5", "format": "json"}, vault=vault)
    if data is None:
        return [], False
    rows = data.get("data", []) if isinstance(data, dict) else []
    out = []
    for r in rows:
        rec = r.get("data", r) if isinstance(r, dict) else {}
        out.append({"name": str(rec.get("NAME", "")).strip(),
                    "city": str(rec.get("CITY", "")).strip(),
                    "state": str(rec.get("STALP", "")).strip(),
                    "active": str(rec.get("ACTIVE", "")) in ("1", "true", "True"),
                    "cert": rec.get("CERT")})
    return out, True


def _fdic_evidence(bank_name: str, vault) -> Evidence:
    matches, reachable = _fdic_lookup(bank_name, vault)
    src = "FDIC BankFind"
    url = f"{FDIC_URL}?search=NAME:{bank_name}".replace(" ", "%20")
    if not reachable:
        return Evidence("fdic_bank", UNAVAILABLE,
                        f"could not reach FDIC to confirm '{bank_name}'",
                        src, 1, "WEB-FDIC-1", url,
                        detail="network unavailable or request failed", query=f"NAME:{bank_name}")
    named = [m for m in matches if _name_matches(bank_name, m["name"])]
    if not named:
        return Evidence("fdic_bank", NOT_FOUND,
                        f"no FDIC-insured institution matching '{bank_name}'",
                        src, 1, "WEB-FDIC-1", url,
                        detail=("top FDIC results: " + "; ".join(m["name"] for m in matches[:3])
                                if matches else "no results"),
                        query=f"NAME:{bank_name}")
    active = [m for m in named if m["active"]]
    # the registry row sharing the MOST meaningful tokens — not the first row
    best = best_match(bank_name, active or named, key=lambda m: m["name"]) \
        or (active or named)[0]
    where = ", ".join(x for x in (best["city"], best["state"]) if x)
    if active:
        return Evidence("fdic_bank", FOUND,
                        f"FDIC lists '{best['name']}'"
                        + (f" ({where})" if where else "") + " as ACTIVE",
                        src, 1, "WEB-FDIC-1", url,
                        detail=f"FDIC CERT {best['cert']}", query=f"NAME:{bank_name}")
    return Evidence("fdic_bank", CONFLICT,
                    f"FDIC record for '{best['name']}' exists but is INACTIVE",
                    src, 1, "WEB-FDIC-1", url,
                    detail="an inactive FDIC record can mean a merger or closure — verify",
                    query=f"NAME:{bank_name}")


def _fed_routing_evidence(aba: str, bank_name: str, vault) -> Evidence | None:
    """Routing number -> owning bank name via a configured Fed routing directory
    connector. Returns None (skip) when the connector is not configured AND the
    checksum already gave structural evidence — we don't want a noisy 'unavailable'
    row for an optional connector unless the operator opted into it."""
    tmpl = os.environ.get("MDMDOC_FED_ROUTING_URL", "").strip()
    aba = _digits(aba)
    src = "Federal Reserve E-Payments Routing Directory"
    if not tmpl:
        return None
    url = tmpl.replace("{aba}", aba)
    data = http.get_json(url, vault=vault)
    if data is None or not isinstance(data, dict):
        return Evidence("fed_routing", UNAVAILABLE,
                        f"routing {aba}: Fed directory connector unreachable",
                        src, 1, "WEB-FED-1", tmpl, query=f"aba:{aba}")
    owner = str(data.get("name") or data.get("customer_name") or "").strip()
    if not owner:
        return Evidence("fed_routing", NOT_FOUND,
                        f"routing {aba} not found in the Fed directory",
                        src, 1, "WEB-FED-1", url, query=f"aba:{aba}")
    if bank_name and not _name_matches(bank_name, owner):
        return Evidence("fed_routing", CONFLICT,
                        f"routing {aba} is owned by '{owner}', "
                        f"but the document's bank is '{bank_name}'",
                        src, 1, "WEB-FED-1", url,
                        detail="name mismatch between routing owner and document bank",
                        query=f"aba:{aba}")
    return Evidence("fed_routing", FOUND,
                    f"routing {aba} is owned by '{owner}'"
                    + (" (matches the document)" if bank_name else ""),
                    src, 1, "WEB-FED-1", url, query=f"aba:{aba}")


def _directory_evidence(aba: str, vault) -> Evidence:
    """Does this routing number EXIST in live public directories? 3-source ladder
    (skill sap-us-bank-validate): usbanklocations (commercial banks) ->
    paymentlabs (also lists government/Treasury/DoD payees usbanklocations
    omits) -> wise (also lists merged/renamed banks the other two miss).
    Real if ANY source finds it; NOT_FOUND only when all three miss AND the
    first two provably answered (their 'not found' pages are HTTP 200, which
    proves the network is up — wise 404s are ambiguous on their own)."""
    aba = _digits(aba)
    src = "usbanklocations + paymentlabs + wise (3-source directory ladder)"

    url1 = f"https://www.usbanklocations.com/routing-number-{aba}.html"
    t1 = http.get_text(url1, vault=vault)
    title1 = ""
    if t1:
        m = re.search(r"<title>([^<]*)</title>", t1, re.I)
        title1 = (m.group(1) if m else "").strip()
        mm = re.match(rf"Bank Routing Number {aba},\s*(.+)$", title1, re.I)
        if mm and "not found" not in title1.lower():
            return Evidence("aba_directory", FOUND,
                            f"routing {aba} is listed: {mm.group(1).strip()}",
                            src, 3, "WEB-DIR-1", url1, query=f"aba:{aba}")
    miss1 = bool(title1) and "not found" in title1.lower()

    url2 = f"https://www.paymentlabs.io/routing/{aba}"
    t2 = http.get_text(url2, vault=vault)
    title2 = ""
    if t2:
        m = re.search(r"<title>([^<]*)</title>", t2, re.I)
        title2 = (m.group(1) if m else "").strip()
        mm = re.match(rf"Payment Labs\s*-\s*{aba}\s*-\s*(.+?)\s+Routing Number", title2, re.I)
        if mm and "not found" not in title2.lower():
            return Evidence("aba_directory", FOUND,
                            f"routing {aba} is listed: {mm.group(1).strip()} "
                            "(government/Treasury payees appear here)",
                            src, 3, "WEB-DIR-1", url2, query=f"aba:{aba}")
    miss2 = bool(title2) and "not found" in title2.lower()

    url3 = f"https://wise.com/us/routing-number/{aba}"
    t3 = http.get_text(url3, vault=vault)
    if t3 and "no longer" not in t3.lower() and "isn't valid" not in t3.lower():
        m = re.search(r"routing number[^.]*?\bfor\b\s+([A-Z][A-Za-z0-9 .,&'\-]{2,50})", t3)
        name = f": {m.group(1).strip()}" if m else " (page exists)"
        return Evidence("aba_directory", FOUND,
                        f"routing {aba} is listed{name} "
                        "(merged/renamed banks appear here)",
                        src, 3, "WEB-DIR-1", url3, query=f"aba:{aba}")

    if miss1 or miss2:
        return Evidence("aba_directory", NOT_FOUND,
                        f"routing {aba} is NOT listed in any of 3 live directories "
                        "— unassigned or retired; verify before payment",
                        src, 3, "WEB-DIR-1", url1,
                        detail=f"checked: {url1} ; {url2} ; {url3}",
                        query=f"aba:{aba}")
    return Evidence("aba_directory", UNAVAILABLE,
                    f"routing {aba}: directory sources unreachable",
                    src, 3, "WEB-DIR-1", url1, query=f"aba:{aba}")


def _routing_evidence(aba: str, which: str, bank_name: str, vault) -> list[Evidence]:
    aba = _digits(aba)
    out: list[Evidence] = []
    src = "ABA routing checksum (FFIEC/Fed algorithm)"
    if len(aba) != 9:
        # not a US ABA at all (Italian ABI/CAB, JP bank/branch codes, …) —
        # a US checksum verdict on a domestic code is noise, not evidence
        return out
    if checksum_valid(aba):
        out.append(Evidence("aba_checksum", FOUND,
                            f"{which} routing {aba} passes the ABA checksum",
                            src, 1, "WEB-ABA-1",
                            detail="structural check only — not proof the account exists",
                            query=f"aba:{aba}"))
    else:
        out.append(Evidence("aba_checksum", CONFLICT,
                            f"{which} routing {aba} FAILS the ABA checksum",
                            src, 1, "WEB-ABA-1",
                            detail="a typo or an invalid number — verify against the bank letter",
                            query=f"aba:{aba}"))
    if checksum_valid(aba):
        # existence only makes sense for well-formed numbers; a checksum-fail
        # is already conclusive and a directory miss would just be noise
        out.append(_directory_evidence(aba, vault))
    fed = _fed_routing_evidence(aba, bank_name, vault)
    if fed is not None:
        out.append(fed)
    return out


def collect(ext) -> list[Evidence]:
    """Bank-document routing evidence. Runs only for US-context banking docs."""
    if ext.doc_class != "bank":
        return []
    f = ext.fields
    out: list[Evidence] = []
    bank_name = str(f.get("bank_name") or "").strip()
    seen: set[str] = set()
    for key, which in (("routing_aba", "standard"), ("routing_aba_wires", "wire")):
        aba = _digits(f.get(key))
        if len(aba) == 9 and aba not in seen:
            seen.add(aba)
            out.extend(_routing_evidence(aba, which, bank_name, ext.vault))
    # FDIC name/active check — only meaningful for US banks; gate on a routing
    # number OR an explicit US bank country to avoid querying non-US banks.
    country = str(f.get("bank_country") or "")
    us_context = bool(seen) or country.strip().upper() in ("US", "USA", "UNITED STATES")
    if bank_name and us_context:
        out.append(_fdic_evidence(bank_name, ext.vault))
    return out
