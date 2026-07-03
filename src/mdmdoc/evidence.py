#!/usr/bin/env python3
"""
evidence.py — on-demand evidence crops for the operator UI.

Resolves WHERE on the original document a piece of evidence lives (page +
relative-coordinate zone) from the persisted run artifacts, and renders the
crop through stage_a._render_zone into a TEMPORARY directory. Crop pixels
carry full sensitive data, so they are streamed to the operator and never
persisted — the same rule as the page-preview endpoint (docs/PRIVACY.md);
the serving endpoint lives on the teach router only (absent in BTP).

Deterministic and model-free: W-9 zones reuse the probe coordinates, the
signature zone is class-specific, bank ID lines are located by searching the
PDF text layer for the extracted value (or its printed label as fallback).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from . import config, runstore
from .fields import _norm_id
from .stage_a import (SCAN_PAGE_CAP, W9_CLASS_ZONE, W9_TIN_ZONE, _render_zone,
                      _SIG_HINTS)

EVIDENCE_KEYS = ("w9_class", "w9_tin", "signature", "iban", "swift_bic",
                 "account_number", "routing_aba", "routing_aba_wires")

# extraction field name -> evidence key (shared by the run page and findings)
FIELD_EVIDENCE = {
    "line3_classification": "w9_class",
    "tin": "w9_tin", "tin_raw": "w9_tin", "tin_type": "w9_tin",
    "signed": "signature", "signature_evidence": "signature", "sign_date": "signature",
    "iban": "iban", "swift_bic": "swift_bic", "account_number": "account_number",
    "routing_aba": "routing_aba", "routing_aba_wires": "routing_aba_wires",
}

# where the signature usually sits (relative page coordinates)
SIGN_ZONE_W9 = (0.0, 0.50, 1.0, 0.82)     # Part II 'Sign Here' block
SIGN_ZONE_BANK = (0.0, 0.45, 1.0, 1.0)    # letters sign in the lower half

# printed-label fallbacks when the value itself can't be located in the text
_LABEL_NEEDLES = {
    "iban": ("iban",),
    "swift_bic": ("swift", "bic"),
    "account_number": ("account number", "account no", "acct", "cuenta",
                       "kontonummer", "konto"),
    "routing_aba": ("routing", "aba"),
    "routing_aba_wires": ("wire",),
}
_BANK_LINE_KEYS = ("iban", "swift_bic", "account_number", "routing_aba",
                   "routing_aba_wires")


def _pub_value(pub_fields: dict, key: str) -> str:
    """Full extracted value when the artifacts carry one (banking dicts under
    the operator display policy, plain strings otherwise). '' when absent."""
    v = pub_fields.get(key)
    if isinstance(v, dict):
        return str(v.get("value") or "")
    if isinstance(v, bool):
        return ""
    return str(v or "")


def _strip_zone(rect, page_rect) -> tuple:
    """Full-width horizontal strip around a located rect, with a little
    vertical context; relative coordinates clamped to the page."""
    h = page_rect.height or 1
    y0 = max(0.0, rect.y0 / h - 0.025)
    y1 = min(1.0, rect.y1 / h + 0.035)
    return (0.0, y0, 1.0, y1)


def _locate_line(doc, pages: list, key: str, value: str) -> tuple | None:
    """(page_idx, zone) of the line carrying a bank identifier. Passes over ALL
    pages, most precise first: exact-value token; long token inside the value
    (the value may be printed in groups); printed label. Text-layer only —
    scans have nothing to search."""
    value_norm = _norm_id(value)
    partial = None
    if value_norm and len(value_norm) >= 4:
        for i in pages:
            try:
                page = doc[i]
                words = page.get_text("words")
            except Exception:
                continue
            for w in words:
                tok = _norm_id(w[4])
                if len(tok) >= 4 and tok == value_norm:
                    return i, _strip_zone(fitz.Rect(w[:4]), page.rect)
                if partial is None and len(tok) >= 5 and tok in value_norm:
                    partial = (i, _strip_zone(fitz.Rect(w[:4]), page.rect))
    if partial:
        return partial
    for i in pages:
        try:
            page = doc[i]
        except Exception:
            continue
        for needle in _LABEL_NEEDLES.get(key, ()):
            try:
                rects = page.search_for(needle)
            except Exception:
                rects = []
            if rects:
                return i, _strip_zone(rects[0], page.rect)
    return None


def _signature_page(doc, doc_class: str, pages_used: list) -> int:
    """Mirror of stage_a's choice: first/last page with a signature phrase,
    else the first/last page the pipeline used."""
    try:
        pages = list(range(min(doc.page_count, SCAN_PAGE_CAP)))
        hits = [i for i in pages if _SIG_HINTS.search(doc[i].get_text() or "")]
        if hits:
            return hits[0] if doc_class == "w9" else hits[-1]
    except Exception:
        pass
    if pages_used:
        return pages_used[0] if doc_class == "w9" else pages_used[-1]
    return 0


def resolve_all(run_id: str) -> dict:
    """Evidence key -> {page (0-based), zone, rotation} for everything
    resolvable on this run. Cheap (text layer only, no rendering) — the run
    page uses it to decide which crops to offer."""
    meta = runstore.load(run_id, "meta.json") or {}
    path = Path(meta.get("path") or "")
    if not path.exists():
        return {}
    ext = path.suffix.lower()
    if ext != ".pdf" and ext not in config.IMAGE_EXTS:
        return {}
    stage_a_pub = runstore.load(run_id, "stage_a.json") or {}
    pub = runstore.load(run_id, "extraction.json") or {}
    doc_class = meta.get("doc_class", "bank")
    doc_type = pub.get("doc_type", "")
    pages_used = [int(p) for p in (stage_a_pub.get("pages_used") or [])]
    rotations = {int(k): v for k, v in (stage_a_pub.get("rotations") or {}).items()}
    first = pages_used[0] if pages_used else 0

    specs: dict = {}
    if doc_class == "w9" and doc_type == "w9" and ext == ".pdf":
        # same coordinates the zone probes read; never for a W-8 (other layout)
        specs["w9_class"] = {"page": first, "zone": W9_CLASS_ZONE,
                             "rotation": rotations.get(first, 0)}
        specs["w9_tin"] = {"page": first, "zone": W9_TIN_ZONE,
                           "rotation": rotations.get(first, 0)}

    if doc_type not in ("invoice", "email", "editable_source"):
        zone = SIGN_ZONE_W9 if doc_class == "w9" else SIGN_ZONE_BANK
        if ext == ".pdf":
            try:
                doc = fitz.open(path)
                idx = _signature_page(doc, doc_class, pages_used)
                doc.close()
            except Exception:
                idx = first
        else:
            idx = 0
        specs["signature"] = {"page": idx, "zone": zone,
                              "rotation": rotations.get(idx, 0)}

    if (doc_class == "bank" and ext == ".pdf" and stage_a_pub.get("has_text_layer")):
        pub_fields = pub.get("fields") or {}
        wanted = [k for k in _BANK_LINE_KEYS
                  if _pub_value(pub_fields, k)
                  or (isinstance(pub_fields.get(k), dict)
                      and pub_fields[k].get("present"))]
        if wanted:
            try:
                doc = fitz.open(path)
                search_pages = pages_used + [i for i in range(min(doc.page_count,
                                                                  SCAN_PAGE_CAP))
                                             if i not in pages_used]
                for k in wanted:
                    hit = _locate_line(doc, search_pages, k,
                                       _pub_value(pub_fields, k))
                    if hit:
                        specs[k] = {"page": hit[0], "zone": hit[1],
                                    "rotation": rotations.get(hit[0], 0)}
                doc.close()
            except Exception:
                pass
    return specs


def render_crop(run_id: str, key: str) -> bytes | None:
    """PNG bytes of the evidence crop, rendered into a temp dir that is
    removed before returning — pixels never persist."""
    if key not in EVIDENCE_KEYS:
        return None
    spec = resolve_all(run_id).get(key)
    if not spec:
        return None
    meta = runstore.load(run_id, "meta.json") or {}
    path = Path(meta.get("path") or "")
    with tempfile.TemporaryDirectory(prefix="mdmdoc-ev-") as td:
        p = _render_zone(path, spec["page"], tuple(spec["zone"]), Path(td),
                         f"ev_{key}", spec.get("rotation", 0))
        return Path(p).read_bytes() if p else None
