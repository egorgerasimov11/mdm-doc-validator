"""Stable entry points for a HOST application (the SAP Consolidator) that embeds
mdmdoc as an optional, locally installed module.

The host imports nothing else from mdmdoc: this module's names and the dict
shapes below are the contract, versioned by API_VERSION. Everything runs
offline — the PDF text layer, tesseract when installed, RapidOCR from bundled
model files — and never needs a model server.

    extract_for_consolidator(path, out_dir=...) → {
        "api_version": 1, "doc_type": "W-9" | "bank confirmation letter" | …,
        "doc_class": "w9" | "bank" | "other", "pages": n, "pages_read": [...],
        "elapsed_s": float, "engines": [...],
        "fields": {key: {"value", "pretty", "status", "page", "bbox_pct", "evidence", "voices"}},
        "extra": {...}, "raw_path": ".../extract.json"}
"""
from __future__ import annotations

import os
from pathlib import Path

API_VERSION = 1

# English-form defaults: tesseract with the English pack gives LINES WITH BOXES
# (tess:auto reads text only), RapidOCR picks its dictionary from the page.
DEFAULT_ENGINES = ["textlayer", "tess:eng", "rapidocr:auto"]

W9_TYPES = ("W-9",)
W8_TYPES = ("W-8BEN-E", "W-8BEN", "W-8ECI", "W-8IMY")
BANK_TYPES = ("RIB (relevé d'identité bancaire)", "ACH / wire authorization form", "voided check",
              "bank statement", "bank confirmation letter", "bankbook / passbook")


class ModuleUnavailable(RuntimeError):
    """No engine can read a page on this machine."""


def capabilities() -> dict:
    """What this install can do — the host shows it next to the module switch."""
    from . import engines as E
    out = {"api_version": API_VERSION, "generic_fields": True, "engines": [], "tesseract": False,
           "rapidocr": False, "offline_models": False,
           "vlm": os.environ.get("MDMDOC_EXTRACT_VLM", "") or None, "reasons": {}}
    for spec in DEFAULT_ENGINES:
        try:
            eng = E.parse(spec)
            ok, why = eng.available()
        except Exception as e:                      # a broken optional dependency is a reason, not a crash
            ok, why = False, f"{e.__class__.__name__}: {e}"
        if ok:
            out["engines"].append(eng.id)
        else:
            out["reasons"][spec] = why
    out["tesseract"] = any(e.startswith("tess") for e in out["engines"])
    out["rapidocr"] = any(e.startswith("rapidocr") for e in out["engines"])
    try:
        out["offline_models"] = bool(E.rapidocr_offline())
    except Exception:
        out["offline_models"] = False
    return out


def doc_class_of(doc_type: str, fields: dict | None = None) -> str:
    if doc_type in W9_TYPES:
        return "w9"
    if doc_type in W8_TYPES:
        return "w8"
    if doc_type in BANK_TYPES:
        return "bank"
    # an unnamed document that carries bank identifiers is bank support
    f = fields or {}
    if any((f.get(k) or {}).get("value") for k in ("iban", "routing_aba", "account_number", "swift_bic")):
        return "bank"
    return "other"


def _seed_rotation(src: Path, cache: Path, pages: list[int]) -> None:
    """The extractor's auto-rotation runs a full CJK tesseract pass per page
    (~15 s) before any engine reads it. A PDF with a usable text layer is
    upright by construction; for the rest one tesseract OSD call (~1 s)
    decides, and without tesseract the page is taken as it comes."""
    from . import render as R
    from .engines import TextLayerEngine, PageJob
    from .. import ocr
    for idx in pages:
        if str(idx) in (R._load_meta(cache).get("rotation") or {}):
            continue
        rot = 0
        try:
            if not R.is_image(src):
                res = TextLayerEngine().transcribe(PageJob(src.stem, src, idx, cache, hints={}, timeout_s=30))
                if (res.meta or {}).get("usable"):
                    R.set_page_rotation(cache, idx, 0)
                    continue
            if ocr.HAVE_TESSERACT:
                from ..stage_a import _osd_rotation
                q = R.render_page(src, cache, idx, R.PRESETS["q120"])
                osd, conf = _osd_rotation(str(q))
                if osd and conf >= 2.0:
                    rot = int(osd)
        except Exception:
            rot = 0
        R.set_page_rotation(cache, idx, rot)


def extract_for_consolidator(path, *, out_dir, engines: list[str] | None = None, vlm: str | None = None,
                             timeout: int = 300, max_pages: int = 20) -> dict:
    """Read the document offline and hand back the schema the host compares.

    Page 0 is read first: a W-9 is its first page (the rest is IRS
    instructions), so the remaining pages are read only for the other kinds."""
    from .extractor import extract_document, guess_doc_type
    from .forms import bank as bank_reader, generic as generic_reader, w9 as w9_reader
    from . import render as R

    src, out_dir = Path(path), Path(out_dir)
    n = R.page_count(src)
    want = list(range(min(n, max(1, int(max_pages)))))
    cache = out_dir / "render"
    cache.mkdir(parents=True, exist_ok=True)
    _seed_rotation(src, cache, want)
    engs = engines or DEFAULT_ENGINES

    def run(pages):
        try:
            return extract_document(src, engines=engs, vlm=vlm or None, out_dir=out_dir,
                                    timeout=timeout, pages=pages)
        except RuntimeError as e:
            if "no engine available" in str(e):
                raise ModuleUnavailable(str(e)) from e
            raise

    doc = run([0])
    if doc.get("doc_type") not in (*W9_TYPES, *W8_TYPES) and len(want) > 1:
        rest = run(want[1:])
        doc["pages_out"] = list(doc["pages_out"]) + list(rest["pages_out"])
        doc["pages_read"] = want
        doc["elapsed_s"] = round(float(doc.get("elapsed_s") or 0) + float(rest.get("elapsed_s") or 0), 1)
        union = "\n".join(pg.get("transcript", "") for pg in doc["pages_out"])
        doc["doc_type"] = guess_doc_type(union)
        doc["transcript"] = union
        import json
        (out_dir / "extract.json").write_text(json.dumps({k: v for k, v in doc.items() if k != "out_dir"},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
    doc_type = doc.get("doc_type") or "unknown"
    fields: dict = {}
    extra: dict = {}
    bank_fields, bank_extra = bank_reader.read(doc)
    klass = doc_class_of(doc_type, bank_fields)
    if klass == "w9":
        page_img = R.render_page(src, cache, 0, R.PRESETS["v200"])
        fields, extra = w9_reader.read(doc, page_image=page_img)
    else:
        # any other document: the bank schema plus the company fields a vendor
        # form also carries (name / address / contacts / tax identifiers)
        fields, extra = generic_reader.read(doc, bank=(bank_fields, bank_extra))
    return {
        "api_version": API_VERSION,
        "doc_type": doc_type, "doc_class": klass,
        "pages": int(doc.get("pages") or n), "pages_read": list(doc.get("pages_read") or pages),
        "elapsed_s": doc.get("elapsed_s"), "engines": list(doc.get("engines") or []),
        "fields": fields, "extra": extra,
        "raw_path": str(Path(doc.get("out_dir") or out_dir) / "extract.json"),
    }


def warm() -> None:
    """Load the OCR engines once (RapidOCR's onnx sessions take ~3 s) so the
    first document does not pay for it. Failures are silent — a missing engine
    is reported by capabilities(), not here."""
    from . import engines as E
    for spec in DEFAULT_ENGINES:
        try:
            eng = E.parse(spec)
            ok, _ = eng.available()
            if ok and hasattr(eng, "_engine") and spec.startswith("rapidocr"):
                for lang in ("latin",):
                    eng._engine(lang)
        except Exception:
            pass


def render_page(path, cache_dir, page: int) -> Path:
    """The page as a JPEG (200 dpi, the frame every bbox_pct refers to)."""
    from . import render as R
    return R.render_page(Path(path), Path(cache_dir), int(page), R.PRESETS["v200"])


def page_count(path) -> int:
    from . import render as R
    return R.page_count(Path(path))
