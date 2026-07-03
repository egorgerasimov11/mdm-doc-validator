#!/usr/bin/env python3
"""
entity.py — legal-entity evidence (MVP item 3): does the account holder / W-9
Line-1 name resolve to a real registered organisation?

  * GLEIF      Tier 1 — the Global LEI index (api.gleif.org, free, no key).
  * SEC EDGAR  Tier 1 — the SEC full-text filer index (efts.sec.gov, free; needs
               a descriptive User-Agent, which http.py sets).

Privacy posture: ONLY organisation names are sent. Personal names (an individual
sole-proprietor's own name) are NOT queried — `_is_org_name` gates on business
suffixes / organisation keywords, so "John Smith" never leaves the machine while
"Acme Holdings LLC" or "American Epilepsy Society" do. No TIN/account/IBAN ever.
"""
from __future__ import annotations

import re

from ..fields import _norm_name, looks_like_business, norm_classification
from . import http
from .evidence import CONFLICT, FOUND, NOT_FOUND, UNAVAILABLE, Evidence

GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"
EDGAR_FTS_URL = "https://efts.sec.gov/LATEST/search-index"

_ORG_KEYWORDS = {
    "society", "foundation", "association", "university", "college", "institute",
    "trust", "fund", "group", "holdings", "partners", "systems", "technologies",
    "solutions", "services", "international", "national", "hospital", "health",
    "medical", "pharma", "bank", "capital", "ventures", "industries", "enterprises",
    "laboratories", "labs", "company", "corporation", "incorporated", "limited",
}


def _is_org_name(name: str, classification: str = "") -> bool:
    """True only for names safe to treat as an ORGANISATION (never a person)."""
    n = str(name or "").strip()
    if len(n) < 3:
        return False
    if looks_like_business(n):
        return True
    if norm_classification(classification) not in ("", "individual_sole_prop"):
        return True
    toks = set(_norm_name(n).split())
    return bool(toks & _ORG_KEYWORDS)


def _gleif(name: str, vault) -> Evidence:
    src, url = "GLEIF", GLEIF_URL
    data = http.get_json(GLEIF_URL, params={"filter[entity.legalName]": name,
                                            "page[size]": "5"}, vault=vault)
    q = f"legalName:{name}"
    if data is None or not isinstance(data, dict):
        return Evidence("gleif_entity", UNAVAILABLE,
                        f"could not reach GLEIF to confirm '{name}'",
                        src, 1, "WEB-GLEIF-1", url, query=q)
    recs = data.get("data", []) or []
    hits = []
    for r in recs:
        attr = (r or {}).get("attributes", {})
        ent = attr.get("entity", {})
        legal = ((ent.get("legalName") or {}).get("name") or "").strip()
        status = str(ent.get("status") or "").upper()
        country = ((ent.get("legalAddress") or {}).get("country") or "").strip()
        lei = attr.get("lei") or (r or {}).get("id") or ""
        if legal:
            hits.append({"name": legal, "status": status, "country": country, "lei": lei})
    named = [h for h in hits if _name_matches(name, h["name"])]
    if not named:
        return Evidence("gleif_entity", NOT_FOUND, f"no GLEIF legal entity matching '{name}'",
                        src, 1, "WEB-GLEIF-1", url,
                        detail=("nearest: " + "; ".join(h["name"] for h in hits[:3])
                                if hits else "no results"), query=q)
    active = [h for h in named if h["status"] in ("", "ACTIVE")]
    best = (active or named)[0]
    link = f"https://search.gleif.org/#/record/{best['lei']}" if best["lei"] else url
    where = f", {best['country']}" if best["country"] else ""
    if not active:
        return Evidence("gleif_entity", CONFLICT,
                        f"GLEIF lists '{best['name']}'{where} but status is {best['status']}",
                        src, 1, "WEB-GLEIF-1", link,
                        detail="a lapsed/retired LEI can mean the entity changed — verify", query=q)
    return Evidence("gleif_entity", FOUND,
                    f"GLEIF confirms '{best['name']}'{where} (LEI {best['lei']})",
                    src, 1, "WEB-GLEIF-1", link, query=q)


def _edgar(name: str, vault) -> Evidence:
    src = "SEC EDGAR"
    q = f'"{name}"'
    data = http.get_json(EDGAR_FTS_URL, params={"q": q}, vault=vault)
    ui = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={name}&output=atom"
    if data is None or not isinstance(data, dict):
        return Evidence("sec_edgar", UNAVAILABLE, f"could not reach SEC EDGAR for '{name}'",
                        src, 1, "WEB-SEC-1", ui, query=f"q:{q}")
    hits = ((data.get("hits") or {}).get("hits")) or []
    names: list[str] = []
    for h in hits:
        srcd = (h or {}).get("_source", {})
        for dn in srcd.get("display_names", []) or []:
            names.append(str(dn))
    matched = [dn for dn in names if _name_matches(name, dn)]
    if matched:
        return Evidence("sec_edgar", FOUND,
                        f"SEC EDGAR has a filer matching '{name}' ({matched[0]})",
                        src, 1, "WEB-SEC-1", ui,
                        detail="the entity files with the SEC (public reporting company)",
                        query=f"q:{q}")
    return Evidence("sec_edgar", NOT_FOUND,
                    f"no SEC EDGAR filer matching '{name}'",
                    src, 1, "WEB-SEC-1", ui,
                    detail="absence is normal — most private companies do not file with the SEC",
                    query=f"q:{q}")


def _name_matches(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    stop = {"the", "of", "and", "co", "company", "inc", "llc", "ltd", "corp", "corporation"}
    core = (set(na.split()) & set(nb.split())) - stop
    return len(core) >= 2


def _candidate_names(ext) -> list[tuple[str, str]]:
    """(name, classification) org names worth checking, deduped."""
    f = ext.fields
    out: list[tuple[str, str]] = []
    if ext.doc_class == "bank":
        out.append((str(f.get("account_holder") or "").strip(), ""))
    else:
        cls = str(f.get("line3_classification") or "")
        out.append((str(f.get("line1_name") or "").strip(), cls))
        out.append((str(f.get("line2_business_name") or "").strip(), cls))
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for name, cls in out:
        key = _norm_name(name)
        if name and key and key not in seen:
            seen.add(key)
            uniq.append((name, cls))
    return uniq


def collect(ext) -> list[Evidence]:
    out: list[Evidence] = []
    for name, cls in _candidate_names(ext):
        if not _is_org_name(name, cls):
            continue
        out.append(_gleif(name, ext.vault))
        out.append(_edgar(name, ext.vault))
    return out
