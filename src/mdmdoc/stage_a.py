#!/usr/bin/env python3
"""
stage_a.py — perception (frozen, untrainable): document -> raw text + deterministic
ID candidates + vision-ready images.

Built to SEARCH, not just read the first page:
  1. survey — every page (up to SCAN_PAGE_CAP) gets a cheap read (text layer, or
     120-DPI tesseract with rotation retry for photos/scans taken sideways);
  2. select — pages are scored by banking/W-9 keyword + regex density and the
     top MAX_PAGES pages win (a 10-page statement with details on page 7 works);
  3. deep read — only the winning pages get the expensive treatment: 300-DPI
     preprocessed tesseract + 170-DPI color vision transcription;
  4. escalate — if a bank doc still shows no account identifiers, a second
     TARGETED vision pass hunts specifically for payment details.

The vision model only TRANSCRIBES here; structured extraction is Stage B's job
(that split is what makes Stage B trainable).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageOps

from . import config, fields, model_client as mc, ocr

SCAN_PAGE_CAP = 12     # survey at most this many pages
QUICK_DPI = 120        # cheap survey render

VISION_TRANSCRIBE_PROMPT = (
    "Transcribe ALL text visible in this document image. Keep labels, numbers, names, "
    "dates and punctuation exactly as printed, line by line. Include text in any language "
    "(Chinese, Korean, Spanish, German...). Output plain text only — no commentary."
)
VISION_TARGETED_PROMPT = (
    "This is a banking/payment document. SEARCH the image for payment details, wherever "
    "and however they are printed (tables, stamps, footers, handwriting, small print): "
    "bank name, account holder/beneficiary, IBAN, account number, SWIFT/BIC, "
    "routing/ABA/sort code, currency. Transcribe each one exactly, with its label. "
    "If a detail is truly absent, say ABSENT."
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
    pages_used: list = field(default_factory=list)   # 0-based indices, score order
    rotations: dict = field(default_factory=dict)    # page index -> degrees applied
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


# --- page survey -------------------------------------------------------------
def _pdf_page_texts(path: Path, cap: int) -> tuple[list, int]:
    try:
        doc = fitz.open(path)
        total = doc.page_count
        texts = [doc[i].get_text() for i in range(min(total, cap))]
        doc.close()
        return texts, total
    except Exception:
        return [], 0


def _quick_ocr(png: str) -> str:
    t = ocr.tesseract_text(png, "eng")
    if ocr.realword_count(t) < 8:
        t2 = ocr.tesseract_text(png, ocr.cjk_lang())
        if len(t2.strip()) > len(t.strip()):
            t = t2
    return t


def _osd_rotation(png_path: str) -> tuple[int, float]:
    """Tesseract orientation detection (one cheap --psm 0 call) -> (rotate, confidence)."""
    import re
    import subprocess
    p = Path(png_path)
    try:
        r = subprocess.run(["tesseract", p.name, "-", "--psm", "0"],
                           capture_output=True, timeout=30, cwd=str(p.parent))
        out = r.stdout + r.stderr
        m = re.search(rb"Rotate:\s*(\d+)", out)
        c = re.search(rb"Orientation confidence:\s*([\d.]+)", out)
        return (int(m.group(1)) % 360 if m else 0,
                float(c.group(1)) if c else 0.0)
    except Exception:
        return 0, 0.0


def _best_rotation(png_path: str, base_text: str) -> tuple[int, str]:
    """Photos come in sideways. OSD detects the orientation; a sideways page
    still yields plenty of GARBAGE pseudo-words, so word counts can't veto a
    confident OSD verdict — apply it directly. Brute-force 90/180/270 only when
    OSD is unsure and the page reads as noise."""
    base_n = ocr.realword_count(base_text)
    p = Path(png_path)
    osd, conf = _osd_rotation(png_path)
    if osd and conf >= 2.0:
        try:
            q = p.with_name(f"{p.stem}.r{osd}.png")
            Image.open(p).rotate(-osd, expand=True).save(q)
            return osd, _quick_ocr(str(q))
        except Exception:
            pass
    if base_n >= 5:
        return 0, base_text
    best_rot, best_text, best_n = 0, base_text, base_n
    for rot in (90, 180, 270):
        try:
            q = p.with_name(f"{p.stem}.r{rot}.png")
            Image.open(p).rotate(-rot, expand=True).save(q)
            t = _quick_ocr(str(q))
            n = ocr.realword_count(t)
            if n > best_n:
                best_rot, best_text, best_n = rot, t, n
        except Exception:
            continue
    return best_rot, best_text


def _survey_scanned_pdf(path: Path, render_dir: Path, doc_class: str) -> list:
    """[(score, page_idx, quick_text, rotation)] over up to SCAN_PAGE_CAP pages."""
    out = []
    try:
        doc = fitz.open(path)
    except Exception:
        return out
    for i in range(min(doc.page_count, SCAN_PAGE_CAP)):
        try:
            pix = doc[i].get_pixmap(dpi=QUICK_DPI)
            png = render_dir / f"q.p{i}.png"
            pix.save(png)
            ImageOps.autocontrast(Image.open(png).convert("L")).save(png)
            t = _quick_ocr(str(png))
            rot, t = _best_rotation(str(png), t)
            out.append((fields.page_score(t, doc_class), i, t, rot))
        except Exception:
            continue
    doc.close()
    return out


def _select_pages(scored: list, max_pages: int) -> list:
    """Top pages by score (score order — best page's text hits the budget first);
    all-zero scores fall back to the first pages."""
    if not scored:
        return []
    ranked = sorted(scored, key=lambda x: (-x[0], x[1]))
    if ranked[0][0] == 0:
        return sorted(scored, key=lambda x: x[1])[:max_pages]
    return ranked[:max_pages]


# --- deep read of the selected pages ------------------------------------------
def _render_page(doc, idx: int, out_dir: Path, dpi: int, rotation: int,
                 grayscale: bool, tag: str) -> str | None:
    try:
        pix = doc[idx].get_pixmap(dpi=dpi)
        p = out_dir / f"{tag}.p{idx}.png"
        pix.save(p)
        im = Image.open(p)
        if rotation:
            im = im.rotate(-rotation, expand=True)
        if grayscale:
            im = ocr._preprocess(im)
        else:
            im = im.convert("RGB")
            if max(im.size) > config.VISION_MAX_SIDE:
                f = config.VISION_MAX_SIDE / max(im.size)
                im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
        im.save(p)
        return str(p)
    except Exception:
        return None


def _deep_read_pages(path: Path, picks: list, render_dir: Path, raw: RawDoc,
                     use_vision: bool) -> None:
    """300-DPI tesseract + 170-DPI vision renders for the selected pages only."""
    try:
        doc = fitz.open(path)
    except Exception:
        return
    tess_parts = []
    for _, idx, _, rot in picks:
        if rot:
            raw.rotations[idx] = rot
        g = _render_page(doc, idx, render_dir, ocr.RENDER_DPI, rot, True, "t")
        if g:
            tess_parts.append(_quick_ocr(g))
        v = _render_page(doc, idx, render_dir, config.VISION_DPI, rot, False, "v")
        if v:
            raw.images.append(v)
    doc.close()
    raw.tesseract_text = "\n".join(tess_parts).strip()
    if use_vision and raw.images:
        vt = mc.vision("VISION", VISION_TRANSCRIBE_PROMPT, raw.images,
                       options={"temperature": 0, "seed": 7})
        if not vt.startswith("[vision"):
            raw.vision_text = vt
        else:
            raw.warnings.append(vt)


def _read_image_file(path: Path, render_dir: Path, raw: RawDoc, use_vision: bool) -> None:
    proc = ocr.prepare_image(path, render_dir)
    t = _quick_ocr(proc)
    rot, t = _best_rotation(proc, t)
    if rot:
        raw.rotations[0] = rot
        Image.open(proc).rotate(-rot, expand=True).save(proc)
    raw.tesseract_text = t
    # vision copy: color, rotated, downscaled
    try:
        vis = Path(proc).with_name("vis.p0.png")
        im = Image.open(path).convert("RGB")
        if rot:
            im = im.rotate(-rot, expand=True)
        if max(im.size) > config.VISION_MAX_SIDE:
            f = config.VISION_MAX_SIDE / max(im.size)
            im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
        im.save(vis)
        raw.images = [str(vis)]
    except Exception:
        raw.images = [proc]
    if use_vision and raw.images:
        vt = mc.vision("VISION", VISION_TRANSCRIBE_PROMPT, raw.images,
                       options={"temperature": 0, "seed": 7})
        if not vt.startswith("[vision"):
            raw.vision_text = vt
        else:
            raw.warnings.append(vt)


def _merged(raw: RawDoc) -> str:
    return "\n".join(x for x in (raw.raw_text, raw.tesseract_text, raw.vision_text) if x)


# --- main entry ----------------------------------------------------------------
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
        texts, raw.pages = _pdf_page_texts(path, SCAN_PAGE_CAP)
        if sum(ocr.realword_count(t) for t in texts) >= 15:
            # text layer: score every page, keep the most relevant ones,
            # best page first so it never falls off the Stage-B budget
            raw.has_text_layer = True
            scored = [(fields.page_score(t, doc_class), i, t, 0) for i, t in enumerate(texts)]
            picks = _select_pages(scored, max_pages)
            raw.pages_used = [i for _, i, _, _ in picks]
            raw.raw_text = "\n".join(t for _, _, t, _ in picks)
        else:
            # scanned: cheap survey of ALL pages (with rotation retry), deep-read the best
            picks = _select_pages(_survey_scanned_pdf(path, render_dir, doc_class), max_pages)
            raw.pages_used = [i for _, i, _, _ in picks]
            _deep_read_pages(path, picks, render_dir, raw, use_vision)
    elif ext in config.IMAGE_EXTS:
        raw.pages = 1
        raw.pages_used = [0]
        _read_image_file(path, render_dir, raw, use_vision)
    else:
        raw.warnings.append(f"unsupported extension {ext}")
        return raw

    if not raw.has_text_layer and not raw.editable and ext not in config.EMAIL_EXTS:
        # prefer the richer transcription as primary text; keep both for regex
        if ocr.realword_count(raw.vision_text) >= ocr.realword_count(raw.tesseract_text):
            raw.raw_text = raw.vision_text or raw.tesseract_text
        else:
            raw.raw_text = raw.tesseract_text
        if not raw.raw_text.strip() and not raw.locked and ext == ".pdf":
            raw.warnings.append("no text recovered (tesseract and vision both empty)")

    raw.regex_candidates = ocr.regex_fields(_merged(raw))

    # escalation: a bank doc with no account identifiers gets a targeted vision hunt
    if (doc_class == "bank" and use_vision and raw.images
            and not any(k in raw.regex_candidates
                        for k in ("iban", "account_number", "routing_aba"))):
        vt2 = mc.vision("VISION", VISION_TARGETED_PROMPT, raw.images,
                        options={"temperature": 0, "seed": 7})
        if not vt2.startswith("[vision"):
            raw.vision_text = (raw.vision_text + "\n\n[targeted search]\n" + vt2).strip()
            raw.regex_candidates = ocr.regex_fields(_merged(raw))
            raw.warnings.append("targeted vision pass used (no IDs found on first read)")
    if use_vision and raw.images:
        mc.unload("VISION")

    if doc_class == "w9" and "ein" not in raw.regex_candidates:
        boxed = fields.find_boxed_tin(_merged(raw))
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
        "pages": raw.pages, "pages_used": raw.pages_used, "rotations": raw.rotations,
        "type_hint": raw.type_hint, "warnings": raw.warnings,
        "raw_text_excerpt": scrub_text(raw.raw_text[:config.EXCERPT_LIMIT], vault),
        "tesseract_chars": len(raw.tesseract_text), "vision_chars": len(raw.vision_text),
        "regex_candidates_masked": cand,
    }
