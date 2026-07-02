#!/usr/bin/env python3
"""
stage_a.py — perception (frozen, untrainable): document -> raw text + deterministic
ID candidates + vision-ready images.

Text-layer PDFs are read with PyMuPDF. Scanned PDFs/images get tesseract at
300 DPI grayscale (ocr.py) AND a vision-model transcription at 170 DPI color —
the vision model only TRANSCRIBES here; structured extraction is Stage B's job
(that split is what makes Stage B trainable).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from . import config, fields, model_client as mc, ocr

VISION_TRANSCRIBE_PROMPT = (
    "Transcribe ALL text visible in this document image. Keep labels, numbers, names, "
    "dates and punctuation exactly as printed, line by line. Include text in any language "
    "(Chinese, Korean, Spanish, German...). Output plain text only — no commentary."
)


@dataclass
class RawDoc:
    path: str
    sha256: str
    ext: str
    doc_class: str                      # "bank" | "w9"
    has_text_layer: bool = False
    locked: bool = False
    editable: bool = False
    pages: int = 0
    raw_text: str = ""                  # FULL text — in-memory only, scrubbed on persist
    tesseract_text: str = ""
    vision_text: str = ""
    regex_candidates: dict = field(default_factory=dict)   # full values in-memory
    images: list = field(default_factory=list)
    type_hint: str = ""
    warnings: list = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.sha256[:16]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdf_text(path: Path, max_pages: int) -> tuple[str, int]:
    """Text layer of the FORM pages only — a W-9 ships with 5 pages of IRS
    instructions that would flood the Stage-B budget and push the filled
    values (which sit at the END of a flattened page's text stream) out."""
    try:
        doc = fitz.open(path)
        pages = doc.page_count
        text = "\n".join(page.get_text() for i, page in enumerate(doc) if i < max_pages)
        doc.close()
        return text, pages
    except Exception:
        return "", 0


def _render_for_vision(path: Path, out_dir: Path, max_pages: int) -> list[str]:
    """170 DPI color renders, downscaled to <=1600 px (qwen2.5vl blanks on huge images)."""
    out: list[str] = []
    ext = path.suffix.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    if ext == ".pdf":
        try:
            doc = fitz.open(path)
        except Exception:
            return out
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            try:
                pix = page.get_pixmap(dpi=config.VISION_DPI)
                p = out_dir / f"vis.p{i}.png"
                pix.save(p)
                out.append(str(p))
            except Exception:
                continue
        doc.close()
    else:
        try:
            p = out_dir / "vis.p0.png"
            Image.open(path).convert("RGB").save(p)
            out.append(str(p))
        except Exception:
            return out
    # downscale in place
    for p in out:
        try:
            im = Image.open(p)
            if max(im.size) > config.VISION_MAX_SIDE:
                f = config.VISION_MAX_SIDE / max(im.size)
                im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
                im.convert("RGB").save(p)
        except Exception:
            continue
    return out


def perceive(path: Path, doc_class: str, render_dir: Path, use_vision: bool = True) -> RawDoc:
    ext = path.suffix.lower()
    raw = RawDoc(path=str(path), sha256=_sha256(path), ext=ext, doc_class=doc_class)
    max_pages = config.MAX_PAGES.get(doc_class, 2)

    if ext in config.EDITABLE_EXTS:
        raw.editable = True
        raw.warnings.append(f"editable source format ({ext}) — not deep-parsed")
        if ext in (".txt", ".csv"):
            try:
                raw.raw_text = path.read_text(errors="replace")[:20000]
            except Exception:
                pass
    elif ext in config.EMAIL_EXTS:
        raw.warnings.append("plain email file — not acceptable bank support by default")
        try:
            raw.raw_text = path.read_text(errors="replace")[:20000]
        except Exception:
            pass
    elif ext == ".pdf":
        if ocr.is_locked_pdf(path):
            raw.locked = True
            raw.warnings.append("password-protected PDF — cannot read")
            return raw
        text, raw.pages = _pdf_text(path, max_pages)
        if ocr.realword_count(text) >= 15:
            raw.has_text_layer = True
            raw.raw_text = text
    elif ext not in config.IMAGE_EXTS:
        raw.warnings.append(f"unsupported extension {ext}")
        return raw

    needs_ocr = (not raw.editable and not raw.locked and ext not in config.EMAIL_EXTS
                 and not raw.has_text_layer and ext in ({".pdf"} | config.IMAGE_EXTS))
    if needs_ocr:
        ores = ocr.read_document(path, render_dir, max_pages=max_pages)
        raw.tesseract_text = ores.get("ocr_text", "")
        if use_vision:
            raw.images = _render_for_vision(path, render_dir, max_pages)
            if raw.images:
                vt = mc.vision("VISION", VISION_TRANSCRIBE_PROMPT, raw.images,
                               options={"temperature": 0, "seed": 7})
                if not vt.startswith("[vision"):
                    raw.vision_text = vt
                else:
                    raw.warnings.append(vt)
                mc.unload("VISION")
        # prefer the richer transcription as primary text; keep both for regex
        if ocr.realword_count(raw.vision_text) >= ocr.realword_count(raw.tesseract_text):
            raw.raw_text = raw.vision_text or raw.tesseract_text
        else:
            raw.raw_text = raw.tesseract_text
        if not raw.raw_text.strip():
            raw.warnings.append("no text recovered (tesseract and vision both empty)")

    merged = "\n".join(x for x in (raw.raw_text, raw.tesseract_text, raw.vision_text) if x)
    raw.regex_candidates = ocr.regex_fields(merged)
    if doc_class == "w9" and "ein" not in raw.regex_candidates:
        boxed = fields.find_boxed_tin(merged)
        if boxed:
            raw.regex_candidates["tin_boxed"] = boxed
    raw.type_hint = fields.type_hint(path.name, raw.raw_text, ext, doc_class)
    return raw


def to_public(raw: RawDoc, vault) -> dict:
    """Persistable (scrubbed) view of Stage A output."""
    from .privacy import FIELD_KIND, mask, scrub_text
    cand = {}
    for k, v in raw.regex_candidates.items():
        if k == "ssn_masked":
            cand[k] = v  # already masked at capture
        else:
            cand[k] = mask(FIELD_KIND.get(k, "account_number"), v) if k in (
                "iban", "account_number", "routing_aba", "ein", "tin_boxed") else v
    return {
        "path": raw.path, "sha256": raw.sha256, "ext": raw.ext, "doc_class": raw.doc_class,
        "has_text_layer": raw.has_text_layer, "locked": raw.locked, "editable": raw.editable,
        "pages": raw.pages, "type_hint": raw.type_hint, "warnings": raw.warnings,
        "raw_text_excerpt": scrub_text(raw.raw_text[:config.EXCERPT_LIMIT], vault),
        "tesseract_chars": len(raw.tesseract_text), "vision_chars": len(raw.vision_text),
        "regex_candidates_masked": cand,
    }
