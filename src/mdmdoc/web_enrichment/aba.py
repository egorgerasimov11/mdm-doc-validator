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

from ..fields import _norm_name
from . import http
from .evidence import CONFLICT, FOUND, NOT_FOUND, UNAVAILABLE, Evidence

FDIC_URL = "https://banks.data.fdic.gov/api/institutions"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def checksum_valid(aba: str) -> bool:
    """Standard ABA routing checksum (mod-10 weighted 3-7-1)."""
    d = _digits(aba)
    if len(d) != 9:
        return False
    n = [int(c) for c in d]
    return (3 * (n[0] + n[3] + n[6]) + 7 * (n[1] + n[4] + n[7])
            + (n[2] + n[5] + n[8])) % 10 == 0


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


def _name_matches(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    stop = {"bank", "na", "n", "a", "the", "of", "trust", "company", "co", "usa"}
    core = (ta & tb) - stop
    return bool(core)


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
    best = (active or named)[0]
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


def _routing_evidence(aba: str, which: str, bank_name: str, vault) -> list[Evidence]:
    aba = _digits(aba)
    out: list[Evidence] = []
    src = "ABA routing checksum (FFIEC/Fed algorithm)"
    if len(aba) != 9:
        out.append(Evidence("aba_checksum", CONFLICT,
                            f"{which} routing '{aba}' is not 9 digits",
                            src, 1, "WEB-ABA-1", detail="malformed routing number",
                            query=f"aba:{aba}"))
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
        if aba and aba not in seen:
            seen.add(aba)
            out.extend(_routing_evidence(aba, which, bank_name, ext.vault))
    # FDIC name/active check — only meaningful for US banks; gate on a routing
    # number OR an explicit US bank country to avoid querying non-US banks.
    country = str(f.get("bank_country") or "")
    us_context = bool(seen) or country.strip().upper() in ("US", "USA", "UNITED STATES")
    if bank_name and us_context:
        out.append(_fdic_evidence(bank_name, ext.vault))
    return out
