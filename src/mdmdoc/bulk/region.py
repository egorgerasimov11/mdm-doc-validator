#!/usr/bin/env python3
"""bulk.region — per-row postal/region checks for BP address exports.

References are ATTACHED PER RUN (Egor's decision): a T005S region-master
export makes region membership checkable; without it only postal formats run
(noted in the result). T005U (region texts) is optional and only enriches
reasons with the region's description.

Rule ids:
  BULK-R01  country unknown / not ISO-mappable                     SUSPICIOUS
  BULK-R02  region code not in T005S for the country               INVALID
  BULK-R03  region empty though the country HAS regions in T005S   SUSPICIOUS
            (region_required countries -> INVALID)
  BULK-R04  postal code does not match the country's format        INVALID
  BULK-R05  postal code present in a country that has none         SUSPICIOUS
  BULK-R06  placeholder region/postal ('Foreign', '99', 'XX')      SUSPICIOUS
  BULK-R07  empty row (no region, no postal)                       SKIPPED
"""
from __future__ import annotations

import re

import yaml

from .. import config
from ..fields import to_iso2
from .model import RowVerdict

_DATA_PATH = config.RULES_DIR / "bulk_postal.yaml"
_PLACEHOLDER_RE = re.compile(r"^(foreign|unknown|n/?a|none|xx+|9{2,}|0{2,}|\.+|-+)$",
                             re.IGNORECASE)


def load_data() -> dict:
    d = yaml.safe_load(_DATA_PATH.read_text(encoding="utf-8")) or {}
    return {"postal_formats": d.get("postal_formats") or {},
            "region_required": {str(c).upper() for c in d.get("region_required") or []}}


def check_rows(rows: list[dict], refs: list | None = None,
               notes: list | None = None, data: dict | None = None,
               progress=None) -> list[RowVerdict]:
    from . import reader
    say = progress or (lambda s: None)
    d = data or load_data()
    notes = notes if notes is not None else []

    # attached references: first T005S-shaped file wins; T005U optional
    regions: dict = {}
    texts: dict = {}
    for ref in refs or []:
        try:
            regions = regions or reader.read_t005s(ref)
            say(f"reference: {getattr(ref, 'name', ref)} -> regions for "
                f"{len(regions)} countries")
            continue
        except reader.BulkInputError:
            pass
        try:
            texts = texts or reader.read_t005u(ref)
            say(f"reference: {getattr(ref, 'name', ref)} -> region texts "
                f"({len(texts)} language-country sets)")
        except reader.BulkInputError:
            notes.append(f"attached reference {getattr(ref, 'name', ref)} was "
                         "not recognized as T005S/T005U — ignored")
    if not regions:
        notes.append("no T005S region reference attached — region membership "
                     "not checked (postal formats only)")

    out: list[RowVerdict] = []
    for i, row in enumerate(rows, start=1):
        if i % 500 == 0:
            say(f"region rows {i}/{len(rows)}")
        rv = RowVerdict(row_no=i, key=str(row.get("partner") or ""))
        raw_cc = str(row.get("country") or "").strip()
        region = str(row.get("region") or "").strip()
        postal = str(row.get("postal_code") or "").strip()

        if not raw_cc and not region and not postal:
            rv.skip("BULK-R07", "empty row — nothing to validate")
            out.append(rv)
            continue

        cc = to_iso2(raw_cc) or (raw_cc.upper() if len(raw_cc) == 2 else "")
        if not cc:
            rv.hit("SUSPICIOUS", "BULK-R01",
                   f"country '{raw_cc or '(empty)'}' is not ISO-mappable — "
                   "region/postal judged blind")

        # placeholders first — они не «неправильный формат», а заглушки
        if region and _PLACEHOLDER_RE.match(region):
            rv.hit("SUSPICIOUS", "BULK-R06", f"region '{region}' is a placeholder")
            region = ""
        if postal and _PLACEHOLDER_RE.match(postal):
            rv.hit("SUSPICIOUS", "BULK-R06", f"postal '{postal}' is a placeholder")
            postal = ""

        # region membership against the attached T005S
        if cc and regions:
            cc_regions = regions.get(cc) or {}
            if region:
                if cc_regions and region not in cc_regions:
                    sample = ", ".join(sorted(cc_regions)[:8])
                    rv.hit("INVALID", "BULK-R02",
                           f"region '{region}' is not a {cc} region in T005S "
                           f"(valid e.g.: {sample})")
                elif cc_regions:
                    desc = (texts.get(("EN", cc), {}) or cc_regions).get(region, "")
                    if desc:
                        rv.reasons.append(f"region {region} = {desc}")
            elif cc_regions:
                bucket = "INVALID" if cc in d["region_required"] else "SUSPICIOUS"
                rv.hit(bucket, "BULK-R03",
                       f"region is empty but {cc} has {len(cc_regions)} regions "
                       "in T005S" + (" (region-required country)"
                                     if cc in d["region_required"] else ""))

        # postal format
        if cc:
            fmt = d["postal_formats"].get(cc)
            if postal:
                if fmt == "":
                    rv.hit("SUSPICIOUS", "BULK-R05",
                           f"{cc} has no postal codes, yet '{postal}' is filled")
                elif fmt and not re.fullmatch(fmt, postal):
                    rv.hit("INVALID", "BULK-R04",
                           f"postal '{postal}' does not match the {cc} "
                           f"format ({fmt})")
        out.append(rv)
    return out
