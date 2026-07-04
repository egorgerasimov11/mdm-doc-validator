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
import re
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
SIGNATURE_PROMPT = (
    "Inspect this document page for a HANDWRITTEN signature (ink strokes) or an ink "
    "stamp/seal. IMPORTANT: a typed or printed name, title or contact block is NOT a "
    "handwritten signature. A handwritten-LOOKING scribble on a scan counts as a "
    "signature even if you cannot prove it is original ink — then say "
    "'signature-like mark present; wet/original cannot be confirmed from a scan' in "
    "evidence. A DocuSign/Adobe-Sign box with a typed name is an ELECTRONIC signature: "
    "handwritten_signature=false, but mention it in evidence. Return strict JSON: "
    '{"handwritten_signature": true/false, "stamp": true/false, '
    '"date_near_signature": "<handwritten/printed date next to the signature, or empty>", '
    '"evidence": "<short phrase describing what you see in the signature area>"}'
)

# --- W-9 zone probes: the checkbox row and the TIN boxes are tiny targets that a
# whole-page vision read keeps missing (real cases: Individual guessed instead of
# a checked S corporation; boxed EIN digits skipped). Cropping the standard-form
# zones and asking pointed questions fixes the perception, not the mapping.
# Relative coordinates of the IRS W-9 (Rev. 2018-2024) layout:
W9_CLASS_ZONE = (0.03, 0.16, 0.82, 0.34)   # x0, y0, x1, y1 fractions
W9_TIN_ZONE = (0.52, 0.40, 1.00, 0.60)
W9_CLASS_PROMPT = (
    "This is the 'federal tax classification' section of IRS Form W-9 with seven "
    "checkboxes: Individual/sole proprietor, C corporation, S corporation, Partnership, "
    "Trust/estate, LLC, Other. Look CAREFULLY at each small square: exactly one should "
    "contain a checkmark, X or filled mark. Return strict JSON: "
    '{"checked": "<the label of the CHECKED box, or none>", '
    '"llc_code": "<C, S or P if written on the LLC line, else empty>", '
    '"evidence": "<what the mark looks like>"}'
)
W9_TIN_PROMPT = (
    "This crop shows Part I of IRS Form W-9: the 'Social security number' boxes and "
    "the 'Employer identification number' boxes. Exactly one group is usually filled "
    "with 9 digits. Read the digits box by box. Return strict JSON: "
    '{"tin_type": "SSN or EIN or none", "digits": "<the 9 digits in order, no separators>", '
    '"evidence": "<which boxes are filled>"}'
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
    bank_letter_pages: list = field(default_factory=list)  # 0-based, packet evidence
    invoice_pages: list = field(default_factory=list)
    signature_probe: dict = field(default_factory=dict)    # vision verdict on signature
    w9_probe: dict = field(default_factory=dict)           # zone probes: checkbox + TIN box
    raw_text: str = ""                  # FULL text — in-memory only, scrubbed on persist
    page_texts: dict = field(default_factory=dict)   # page idx -> its text; in-memory only
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
            t = _quick_ocr(g)
            tess_parts.append(t)
            raw.page_texts[idx] = t
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


def _collect_markers(raw: RawDoc, indexed_texts: list, doc_class: str) -> None:
    """Per-page packet evidence: which pages look like a bank confirmation
    letter and which like an invoice. Drives packet-aware classification."""
    if doc_class != "bank":
        return
    for i, t in indexed_texts:
        m = fields.page_markers(t)
        if m["bank_letter"]:
            raw.bank_letter_pages.append(i)
        if m["invoice"]:
            raw.invoice_pages.append(i)


_SIG_HINTS = re.compile(r"(?i)sincerely|signature of|sign here|authorized signature|"
                        r"certification|firma|assinatura|unterschrift|подпись")


def _signature_page(path: Path, raw: RawDoc) -> int:
    """Which page most likely holds the signature. W-9: the certification page
    (usually 1). Letters: the last page mentioning a signature phrase."""
    try:
        doc = fitz.open(path)
        pages = list(range(min(doc.page_count, SCAN_PAGE_CAP)))
        hits = [i for i in pages if _SIG_HINTS.search(doc[i].get_text() or "")]
        doc.close()
        if hits:
            return hits[0] if raw.doc_class == "w9" else hits[-1]
    except Exception:
        pass
    if raw.pages_used:
        return raw.pages_used[0] if raw.doc_class == "w9" else raw.pages_used[-1]
    return 0


def signature_probe(path: Path, raw: RawDoc, render_dir: Path) -> None:
    """Vision check for a real (wet) signature/stamp. Signatures are image
    overlays — invisible to the text layer — so this runs for text-layer PDFs
    too. Called while VISION is still resident (no extra model swap)."""
    try:
        idx = 0
        if path.suffix.lower() == ".pdf":
            doc = fitz.open(path)
            idx = _signature_page(path, raw)
            png = _render_page(doc, idx, render_dir, config.VISION_DPI,
                               raw.rotations.get(idx, 0), False, "sig")
            doc.close()
        else:
            png = raw.images[0] if raw.images else None
        if not png:
            return
        obj, _ = mc.generate_json_vision(SIGNATURE_PROMPT, [png])
        if isinstance(obj, dict):
            raw.signature_probe = {
                "handwritten_signature": bool(obj.get("handwritten_signature")),
                "stamp": bool(obj.get("stamp")),
                "evidence": str(obj.get("evidence") or "")[:200],
                "page": idx,
            }
    except Exception as e:  # noqa: BLE001 — probe is best-effort
        raw.warnings.append(f"signature probe failed ({e.__class__.__name__})")


def _render_zone(path: Path, page_idx: int, zone: tuple, render_dir: Path,
                 tag: str, rotation: int = 0) -> str | None:
    """Render one page and crop a relative-coordinate zone, upscaled for the
    vision model (small crops read far better than full pages)."""
    try:
        doc = fitz.open(path)
        if page_idx >= doc.page_count:
            page_idx = 0
        pix = doc[page_idx].get_pixmap(dpi=220)
        doc.close()
        p = render_dir / f"{tag}.png"
        pix.save(p)
        im = Image.open(p)
        if rotation:
            im = im.rotate(-rotation, expand=True)
        w, h = im.size
        x0, y0, x1, y1 = zone
        im = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
        if max(im.size) < 1100:
            f = 1100 / max(im.size)
            im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
        im.convert("RGB").save(p)
        return str(p)
    except Exception:
        return None


def w9_zone_probe(path: Path, raw: RawDoc, render_dir: Path) -> None:
    """Targeted vision reads of the W-9 checkbox row and TIN boxes. Runs while
    VISION is resident. Digits stay in memory (registered later as secrets)."""
    if path.suffix.lower() not in (".pdf", *config.IMAGE_EXTS):
        return
    page = raw.pages_used[0] if raw.pages_used else 0
    rot = raw.rotations.get(page, 0)
    probe: dict = {}
    crop = _render_zone(path, page, W9_CLASS_ZONE, render_dir, "z_class", rot) \
        if path.suffix.lower() == ".pdf" else None
    if crop:
        obj, _ = mc.generate_json_vision(W9_CLASS_PROMPT, [crop])
        if isinstance(obj, dict) and str(obj.get("checked") or "").strip().lower() not in ("", "none"):
            probe["classification"] = str(obj["checked"]).strip()
            probe["llc_code"] = str(obj.get("llc_code") or "").strip()
            probe["class_evidence"] = str(obj.get("evidence") or "")[:160]
    crop = _render_zone(path, page, W9_TIN_ZONE, render_dir, "z_tin", rot) \
        if path.suffix.lower() == ".pdf" else None
    if crop:
        obj, _ = mc.generate_json_vision(W9_TIN_PROMPT, [crop])
        if isinstance(obj, dict):
            digits = re.sub(r"\D", "", str(obj.get("digits") or ""))
            ttype = str(obj.get("tin_type") or "").upper()
            if len(digits) == 9 and ttype in ("SSN", "EIN"):
                probe["tin_type"] = ttype
                probe["tin_digits"] = digits          # FULL — memory only
                probe["tin_evidence"] = str(obj.get("evidence") or "")[:160]
    if probe:
        probe["page"] = page
    raw.w9_probe = probe


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
            _collect_markers(raw, list(enumerate(texts)), doc_class)
            scored = [(fields.page_score(t, doc_class), i, t, 0) for i, t in enumerate(texts)]
            picks = _select_pages(scored, max_pages)
            raw.pages_used = [i for _, i, _, _ in picks]
            raw.page_texts = {i: t for _, i, t, _ in picks}
            raw.raw_text = "\n".join(t for _, _, t, _ in picks)
        else:
            # scanned: cheap survey of ALL pages (with rotation retry), deep-read the best
            survey = _survey_scanned_pdf(path, render_dir, doc_class)
            _collect_markers(raw, [(i, t) for _, i, t, _ in survey], doc_class)
            picks = _select_pages(survey, max_pages)
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
    # signature probe — ALWAYS for bank/w9 (quality first), even for text-layer
    # PDFs (a wet signature is pixels, not text). Skipped for hard-reject types.
    if (use_vision and not raw.editable and not raw.locked
            and ext not in config.EMAIL_EXTS and raw.type_hint != "invoice"):
        signature_probe(path, raw, render_dir)
        # W-9 zone probes: checkbox + TIN boxes (tiny targets whole-page vision
        # keeps missing) — while VISION is still resident. NOT for W-8: its
        # layout differs, the W-9 zone coordinates would read random areas.
        if doc_class == "w9" and raw.type_hint != "w8":
            w9_zone_probe(path, raw, render_dir)
    if use_vision:
        mc.unload("VISION")

    if doc_class == "w9" and "ein" not in raw.regex_candidates:
        boxed, boxed_type = fields.find_boxed_tin(_merged(raw))
        if boxed:
            raw.regex_candidates["tin_boxed"] = boxed
            if boxed_type:
                raw.regex_candidates["tin_boxed_type"] = boxed_type
    raw.type_hint = fields.type_hint(path.name, raw.raw_text, ext, doc_class)
    # single-page docs: everything read (incl. vision text) belongs to that page —
    # gives provenance a page even when per-page capture missed (in-memory only)
    if len(raw.pages_used) == 1:
        raw.page_texts[raw.pages_used[0]] = _merged(raw)
    return raw


def to_public(raw: RawDoc, vault, policy: str = "masked") -> dict:
    """Persistable view of Stage A output. policy='full' shows banking values in
    full; TIN kinds (ein/tin_boxed/ssn) stay masked under every policy."""
    from .privacy import FIELD_KIND, display_value, scrub_text
    cand = {}
    for k, v in raw.regex_candidates.items():
        if k == "ssn_masked":
            cand[k] = v  # already masked at capture
        elif k in ("iban", "account_number", "routing_aba", "routing_aba_wires",
                   "ein", "tin_boxed"):
            cand[k] = display_value(FIELD_KIND.get(k, "account_number"), v, policy)
        else:
            cand[k] = v
    scrub_policy = "tin-only" if policy == "full" else "strict"
    return {
        "path": raw.path, "sha256": raw.sha256, "ext": raw.ext, "doc_class": raw.doc_class,
        "has_text_layer": raw.has_text_layer, "locked": raw.locked, "editable": raw.editable,
        "pages": raw.pages, "pages_used": raw.pages_used, "rotations": raw.rotations,
        "bank_letter_pages": raw.bank_letter_pages, "invoice_pages": raw.invoice_pages,
        "type_hint": raw.type_hint, "warnings": raw.warnings,
        "raw_text_excerpt": scrub_text(raw.raw_text[:config.EXCERPT_LIMIT], vault,
                                       policy=scrub_policy),
        "tesseract_chars": len(raw.tesseract_text), "vision_chars": len(raw.vision_text),
        "regex_candidates_masked": cand,
        "w9_probe": {k: v for k, v in raw.w9_probe.items() if k != "tin_digits"} or None,
    }


_W9_SNIFF = ("form w-9", "request for taxpayer", "taxpayer identification number",
             "w-8ben", "w-8ben-e", "certificate of foreign status", "substitute w-9",
             "substitute form w-9")
_W9_NAME_RE = re.compile(r"(?i)\bw[-_ ]?(9|8(ben)?)\b")


def sniff_doc_class(path: Path) -> str:
    """Cheap upfront guess for the single 'Auto' entry point: W-9/W-8 tax form
    vs banking document. Filename first, then the text layer, then ONE fast OCR
    of page 1 for image-only files. W-9 markers are explicit, so 'bank' is the
    safe default when nothing matches."""
    if _W9_NAME_RE.search(path.name):
        return "w9"
    ext = path.suffix.lower()
    text = ""
    try:
        if ext == ".pdf":
            doc = fitz.open(path)
            text = "".join(doc[i].get_text() for i in range(min(doc.page_count, 2)))
            doc.close()
        elif ext in config.EMAIL_EXTS or ext in (".txt", ".csv"):
            text = path.read_text(errors="replace")[:8000]
    except Exception:
        text = ""
    if len(text.strip()) < 40 and (ext == ".pdf" or ext in config.IMAGE_EXTS):
        # image-only: one low-cost tesseract pass over page 1, classification only
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                pngs = (ocr.render_pdf_pages(path, Path(td), max_pages=1)
                        if ext == ".pdf" else [ocr.prepare_image(path, Path(td))])
                text = ocr.tesseract_text(pngs[0]) if pngs else ""
        except Exception:
            text = ""
    t = text.lower()
    if any(k in t for k in _W9_SNIFF):
        return "w9"
    return "bank"
