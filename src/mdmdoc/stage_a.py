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
HOLDER_TARGETED_PROMPT = (
    "This is a banking document. Find WHO OWNS the account — the account "
    "holder / beneficiary / account name (usually a company, often with a legal "
    "suffix like GmbH, S.A.S., Ltd, 株式会社). Check labels in any language "
    "('account holder', 'beneficiary', 'a nombre de', 'titular', 'Kontoinhaber', "
    "'intestato a', '口座名義', '户名'), the addressee block and the letterhead. "
    "Also transcribe the BANK's name. Answer with labels, e.g. "
    "'Account holder: ...' and 'Bank: ...'. If truly absent, say ABSENT."
)
# holder-label tokens — when NONE of these appear in the recovered text, the
# holder is invisible to the text path and only a vision re-read can find it
_HOLDER_LABELS_RE = None  # compiled lazily below


def _holder_labels_present(text: str) -> bool:
    global _HOLDER_LABELS_RE
    if _HOLDER_LABELS_RE is None:
        import re as _re
        _HOLDER_LABELS_RE = _re.compile(
            r"account\s*holder|beneficiary|account\s*name|in\s+the\s+name\s+of|"
            r"a\s+nombre\s+de|titular|kontoinhaber|intestat|口座名義|户名|戶名",
            _re.IGNORECASE)
    return bool(_HOLDER_LABELS_RE.search(text or ""))
SIGNATURE_PROMPT = (
    "Inspect this document page for a HANDWRITTEN signature (ink strokes) or an ink "
    "stamp/seal. IMPORTANT: a typed or printed name, title or contact block is NOT a "
    "handwritten signature. A handwritten-LOOKING scribble on a scan counts as a "
    "signature even if you cannot prove it is original ink — then say "
    "'signature-like mark present; wet/original cannot be confirmed from a scan' in "
    "evidence. A DocuSign/Adobe-Sign box with a typed name is an ELECTRONIC signature: "
    "handwritten_signature=false, but mention it in evidence. A REGULATOR watermark or "
    "margin stamp (e.g. 'VIGILADO Superintendencia Financiera') is NOT a signature "
    "stamp: stamp=false, mention it in evidence. Return strict JSON: "
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
# Line 1 + Line 2 name boxes sit directly under the form header on both
# revisions. Positional read kills the line1<->line2 swap by construction
# (real case: line1 came back with line2's DBA and the owner name was lost).
# y-range probed live on Rev 3-2024: 0.06-0.18 cuts the boxes off; 0.09-0.22
# reads both values (2018-rev boxes at ~0.10-0.16 stay inside too).
W9_NAME_ZONE = (0.03, 0.09, 0.97, 0.22)
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
W9_NAME_PROMPT = (
    "This crop shows the TOP of IRS Form W-9: box 1 'Name of entity/individual' "
    "and box 2 'Business name/disregarded entity name, if different from above'. "
    "Read ONLY the handwritten/typed VALUES the taxpayer entered — never the "
    "printed captions. A box may legitimately be empty. Do NOT copy box 2's "
    "value into box 1. Return strict JSON: "
    '{"line1_name": "<value in box 1, or empty>", '
    '"line2_business_name": "<value in box 2, or empty>", '
    '"evidence": "<which boxes hold entered values>"}'
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
    w9_pages: list = field(default_factory=list)     # 0-based: W-9/W-8 form pages in a bank packet
    # cheap per-page survey text for ALL surveyed pages (idx -> text); feeds the
    # evidence ladder / signature-page hints. In-memory only, NEVER persisted.
    survey_texts: dict = field(default_factory=dict)
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
    # P2: a text layer existed but was unreadable (encoded font) — the doc was
    # rerouted to the scan branch; recorded for eval/review observability
    text_layer_garbage: bool = False
    # P4: the W-8 certification (Part XXX) page index — the signature probe
    # targets it instead of guessing from the first selected page
    w8_cert_page: int | None = None

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
        # chunked transcribe (P1): several full-page renders in ONE call
        # overflow the vision context (real 400 on an 8-page W-8 scan)
        step = max(1, mc.VISION_MAX_IMAGES_PER_CALL)
        parts = []
        for i in range(0, len(raw.images), step):
            vt = mc.vision("VISION", VISION_TRANSCRIBE_PROMPT, raw.images[i:i + step],
                           options={"temperature": 0, "seed": 7})
            if not vt.startswith("[vision"):
                parts.append(vt)
            else:
                raw.warnings.append(vt)
            if mc.LAST_VISION_NOTE:
                raw.warnings.append(mc.LAST_VISION_NOTE)
        raw.vision_text = "\n".join(parts).strip()


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
    letter, an invoice, or a W-9-family tax form. Drives packet-aware
    classification and page-scoped signature handling (P3)."""
    if doc_class != "bank":
        return
    for i, t in indexed_texts:
        m = fields.page_markers(t)
        if m["bank_letter"]:
            raw.bank_letter_pages.append(i)
        if m["invoice"]:
            raw.invoice_pages.append(i)
        if m["w9_form"]:
            raw.w9_pages.append(i)


_SIG_HINTS = re.compile(r"(?i)sincerely|signature of|sign here|authorized signature|"
                        r"certification|firma|assinatura|unterschrift|подпись")

# --- W-8 required-page targeting (P4) ----------------------------------------
# A W-8BEN-E is an 8-page form whose evidence lives on SPECIFIC pages: Part I
# (identification) up front, the chapter-4 certification Part in the middle,
# and the Part XXX certification/signature at the END. Generic keyword scoring
# has no W-8 awareness — these patterns pick the required pages from the cheap
# survey/page texts at zero model cost.
_W8_DOC_RE = re.compile(r"(?i)w[-_ ]?8[\s-]?ben(-?e)?|certificate of foreign status")
_W8_PART1_RE = re.compile(r"(?i)identification of beneficial owner")
_W8_CERT_XXX_RE = re.compile(r"(?i)part\s+xxx\b")
_W8_SIGNHERE_RE = re.compile(r"(?i)sign\s*here")
_W8_CERT_WORD_RE = re.compile(r"(?i)\bcertification\b")


def _select_w8_pages(scored: list, max_pages: int) -> tuple[list, int]:
    """scored = [(page_score, idx, text, rot)] over ALL surveyed pages.
    -> (picks ordered [Part I, certification, best-remaining], cert_idx).
    part1: first page matching _W8_PART1_RE, else idx 0.
    cert priority ladder: first Part-XXX page; else first 'Sign Here' page;
    else the LAST 'Certification' page; else the last surveyed page.
    best-remaining: highest page_score among the rest (ties -> lowest idx)."""
    by_idx = {i: (s, i, t, r) for s, i, t, r in scored}
    idxs = sorted(by_idx)
    if not idxs:
        return [], 0

    def _first(rx) -> int | None:
        for i in idxs:
            if rx.search(by_idx[i][2] or ""):
                return i
        return None

    def _last(rx) -> int | None:
        for i in reversed(idxs):
            if rx.search(by_idx[i][2] or ""):
                return i
        return None

    part1 = _first(_W8_PART1_RE)
    part1 = part1 if part1 is not None else idxs[0]
    cert = _first(_W8_CERT_XXX_RE)
    if cert is None:
        cert = _first(_W8_SIGNHERE_RE)
    if cert is None:
        cert = _last(_W8_CERT_WORD_RE)
    if cert is None:
        cert = idxs[-1]
    picks = [by_idx[part1]]
    if cert != part1:
        picks.append(by_idx[cert])
    rest = sorted((by_idx[i] for i in idxs if i not in (part1, cert)),
                  key=lambda x: (-x[0], x[1]))
    for row in rest:
        if len(picks) >= max_pages:
            break
        picks.append(row)
    return picks[:max_pages], cert


def _w8_detected(path: Path, texts: list, doc_class: str) -> bool:
    """Deterministic page-SELECTION input (P4) — deliberately separate from
    raw.type_hint, which stays frozen post-selection (F1 invariant)."""
    if doc_class != "w9":
        return False
    if _W8_DOC_RE.search(path.name):
        return True
    return any(_W8_DOC_RE.search(t or "") for t in texts)


def _signature_page(path: Path, raw: RawDoc) -> int:
    """Which page most likely holds the signature. W-8: the certification
    (Part XXX) page found during page targeting (P4). W-9: the certification
    page (usually 1). Letters: the last page mentioning a signature phrase —
    survey text is consulted too, so SCANS get real hints instead of the
    blind pages_used fallback; W-9-marked pages of a bank packet are skipped
    (their signature belongs to the tax form, not the letter — P3/D)."""
    if raw.w8_cert_page is not None:
        return raw.w8_cert_page
    hits: list[int] = []
    try:
        doc = fitz.open(path)
        pages = list(range(min(doc.page_count, SCAN_PAGE_CAP)))
        for i in pages:
            text = raw.survey_texts.get(i) or (doc[i].get_text() or "")
            if _SIG_HINTS.search(text):
                hits.append(i)
        doc.close()
    except Exception:
        pass
    if raw.doc_class == "bank" and raw.w9_pages:
        non_w9 = [i for i in hits if i not in raw.w9_pages]
        if non_w9:
            hits = non_w9
        elif hits:
            # every hinted page is a W-9 page: prefer a non-W9 page we read
            fallback = [p for p in raw.pages_used if p not in raw.w9_pages]
            if fallback:
                return fallback[-1]
    if hits:
        return hits[0] if raw.doc_class == "w9" else hits[-1]
    if raw.pages_used:
        return raw.pages_used[0] if raw.doc_class == "w9" else raw.pages_used[-1]
    return 0


def _signature_band(path: Path, idx: int, doc_class: str,
                    is_w8_cert: bool = False) -> tuple | None:
    """Zone (x0,y0,x1,y1 fractions) where the signature most likely sits.
    Text-layer pages: anchored on the y-position of the _SIG_HINTS line (a
    cursive mark is a sub-1%-of-page target on a dense letter — the same
    failure mode the W-9 zones fixed). Scans: bottom third for letters,
    the Part II certification region for W-9s, the Part XXX sign block
    (bottom ~third) for a W-8 certification page (S2)."""
    try:
        doc = fitz.open(path)
        page = doc[idx]
        h = page.rect.height or 1
        y_frac = None
        for w in page.get_text("words"):
            if _SIG_HINTS.search(w[4] or ""):
                y_frac = w[1] / h    # top of the hint word
        doc.close()
        if y_frac is not None:
            return (0.0, max(0.0, y_frac - 0.05), 1.0, min(1.0, y_frac + 0.28))
    except Exception:
        pass
    if is_w8_cert:
        return (0.0, 0.60, 1.0, 0.95)
    return (0.0, 0.50, 1.0, 0.85) if doc_class == "w9" else (0.0, 0.60, 1.0, 1.0)


def _sig_vote(o) -> str:
    """pos-hand | pos-stamp | neg | err — an unusable reply is ERR, never a
    negative (S2: the old probe collapsed model errors into 'neg')."""
    if not isinstance(o, dict):
        return "err"
    if o.get("handwritten_signature"):
        return "pos-hand"
    if o.get("stamp"):
        return "pos-stamp"
    return "neg"


def signature_probe(path: Path, raw: RawDoc, render_dir: Path) -> None:
    """Vision ENSEMBLE for a real (wet) signature/stamp (S2, subsumed R4).
    Signatures are image overlays — invisible to the text layer — so this runs
    for text-layer PDFs too, while VISION is resident.

    * e-sign short-circuit: text markers say electronically signed -> ZERO
      vision calls (the fold is settled deterministically in stage_b).
    * band-first, page fallback on a band negative (as before).
    * escalations (each once, cap-guarded, only on genuine disagreement):
      E-A band-neg/page-pos -> re-probe the band at 300 DPI;
      E-B bank stamp-only positives -> bottom-third crop (hunt handwriting);
      E-C typed-system text vs a positive vote -> one extra probe.
    * budget: MDMDOC_SIG_VISION_CAP (default 4) counts every probe call;
      err votes never escalate (vision is unhealthy — do not burn budget).
    * fold: positives > negatives -> positive; TIE -> negative (unsigned is
      the safe direction); contested (pos and neg both present) is a HARD
      confidence signal downstream."""
    votes = {"band": "not-run", "page": "not-run", "band_hi": "not-run",
             "crop_b3": "not-run", "text": _text_signature_vote(raw)}
    trail: list[dict] = []
    if votes["text"] == "esign":
        raw.signature_probe = {
            "handwritten_signature": False, "stamp": False,
            "date_near_signature": "",
            "evidence": "e-signature markers in text — vision probes skipped",
            "page": None, "votes": votes, "trail": trail,
            "uncertain": False, "esign_short_circuit": True,
        }
        return
    try:
        cap = config.sig_vision_cap()
        calls = 0
        objs: dict[str, dict] = {}

        def _probe(name: str, image: str | None) -> str:
            nonlocal calls
            if not image or calls >= cap:
                return "not-run"
            calls += 1
            obj, _ = mc.generate_json_vision(SIGNATURE_PROMPT, [image])
            v = _sig_vote(obj)
            votes[name] = v
            if isinstance(obj, dict):
                objs[name] = obj
            trail.append({"probe": name, "vote": v,
                          "evidence": str((obj or {}).get("evidence") or "")[:120]
                          if isinstance(obj, dict) else ""})
            return v

        idx = 0
        band = None
        is_pdf = path.suffix.lower() == ".pdf"
        if is_pdf:
            idx = _signature_page(path, raw)
            rot = raw.rotations.get(idx, 0)
            band = _signature_band(path, idx, raw.doc_class,
                                   is_w8_cert=(raw.w8_cert_page == idx))
            band_png = _render_zone(path, idx, band, render_dir, "sig", rot)
            if band_png:
                if _probe("band", band_png) in ("neg", "err") and votes["band"] == "neg":
                    doc = fitz.open(path)
                    png = _render_page(doc, idx, render_dir, config.VISION_DPI,
                                       rot, False, "sig")
                    doc.close()
                    _probe("page", png)
            else:
                doc = fitz.open(path)
                png = _render_page(doc, idx, render_dir, config.VISION_DPI,
                                   rot, False, "sig")
                doc.close()
                _probe("page", png)
        else:
            _probe("page", raw.images[0] if raw.images else None)

        usable = [v for v in votes.values()
                  if v in ("pos-hand", "pos-stamp", "neg")]
        errs = sum(1 for k, v in votes.items() if k != "text" and v == "err")

        # escalations — only when vision is healthy (no err votes)
        if is_pdf and not errs:
            pos = [v for v in usable if v.startswith("pos")]
            neg = [v for v in usable if v == "neg"]
            if votes["band"] == "neg" and votes["page"].startswith("pos"):
                # E-A: the two base reads disagree — re-read the band hi-res
                hi = _render_zone(path, idx, band, render_dir, "sig_hi",
                                  raw.rotations.get(idx, 0), dpi=300)
                _probe("band_hi", hi)
            elif (raw.doc_class == "bank" and pos
                    and all(v == "pos-stamp" for v in pos)):
                # E-B: stamp-only positives — hunt handwriting in the bottom third
                b3 = _render_zone(path, idx, (0.0, 0.66, 1.0, 1.0), render_dir,
                                  "sig_b3", raw.rotations.get(idx, 0))
                _probe("crop_b3", b3)
            elif votes["text"] == "typed-system" and pos:
                # E-C: deterministic 'no signature required' vs a positive vote
                hi = _render_zone(path, idx, band, render_dir, "sig_hi",
                                  raw.rotations.get(idx, 0), dpi=300)
                _probe("band_hi", hi)

        usable = [v for v in votes.values()
                  if v in ("pos-hand", "pos-stamp", "neg")]
        pos_n = sum(1 for v in usable if v.startswith("pos"))
        neg_n = sum(1 for v in usable if v == "neg")
        if not usable:
            # every attempted probe errored (or none could run on an image-less
            # doc): NOT a negative read — unverified, confidence holds ACCEPTs
            attempted = any(v == "err" for k, v in votes.items() if k != "text")
            if attempted:
                raw.signature_probe = {
                    "handwritten_signature": False, "stamp": False,
                    "date_near_signature": "", "evidence": "",
                    "page": idx, "votes": votes, "trail": trail,
                    "uncertain": True, "no_visual_verdict": True,
                    "reason": "vision-unavailable-or-unusable",
                }
            return
        positive = pos_n > neg_n                    # TIE -> negative (safe)
        contested = pos_n >= 1 and neg_n >= 1
        # the decisive object: first positive probe when positive, else last usable
        decisive = None
        for name in ("band", "page", "band_hi", "crop_b3"):
            o = objs.get(name)
            if o is None:
                continue
            if positive and _sig_vote(o).startswith("pos"):
                decisive = o
                break
            decisive = o
        decisive = decisive or {}
        uncertain = contested or (votes["text"] == "typed-system" and positive)
        raw.signature_probe = {
            "handwritten_signature": positive and bool(decisive.get("handwritten_signature")),
            "stamp": positive and bool(decisive.get("stamp")),
            "date_near_signature": str(decisive.get("date_near_signature") or "")[:40],
            "evidence": str(decisive.get("evidence") or "")[:200],
            "page": idx, "votes": votes, "trail": trail,
            "uncertain": bool(uncertain), "contested": bool(contested),
            "escalated": any(votes[k] != "not-run" for k in ("band_hi", "crop_b3")),
            "probes_used": calls, "cap": cap,
        }
    except Exception as e:  # noqa: BLE001 — probe is best-effort
        raw.warnings.append(f"signature probe failed ({e.__class__.__name__})")
        raw.signature_probe = {
            "handwritten_signature": False, "stamp": False,
            "date_near_signature": "", "evidence": "", "page": 0,
            "votes": votes, "trail": trail,
            "uncertain": True, "no_visual_verdict": True,
            "reason": f"probe-exception:{e.__class__.__name__}",
        }


# shared with stage_b's esign guard and the ABAP twin (S1); kept as module
# aliases so existing references/tests stay valid
_ESIGN_TOKENS = fields.ESIGN_TOKENS
_TYPED_SYSTEM_TOKENS = fields.TYPED_SYSTEM_TOKENS


def _text_signature_vote(raw: RawDoc) -> str:
    """Deterministic signature signal from the PRE-hunt document text:
    'esign' (an e-signature envelope is a signature), 'typed-system' (a
    computer-generated notice that states it needs no signature), or 'none'."""
    low = (raw.raw_text or "").lower()
    if any(t in low for t in _ESIGN_TOKENS):
        return "esign"
    if any(t in low for t in _TYPED_SYSTEM_TOKENS):
        return "typed-system"
    return "none"


def _render_zone(path: Path, page_idx: int, zone: tuple, render_dir: Path,
                 tag: str, rotation: int = 0, dpi: int = 220) -> str | None:
    """Render one page and crop a relative-coordinate zone, upscaled for the
    vision model (small crops read far better than full pages). dpi=300 is
    the S2 hi-res escalation variant."""
    try:
        doc = fitz.open(path)
        if page_idx >= doc.page_count:
            page_idx = 0
        pix = doc[page_idx].get_pixmap(dpi=dpi)
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
    crop = _render_zone(path, page, W9_NAME_ZONE, render_dir, "z_name", rot) \
        if path.suffix.lower() == ".pdf" else None
    if crop:
        obj, _ = mc.generate_json_vision(W9_NAME_PROMPT, [crop])
        if isinstance(obj, dict):
            l1 = str(obj.get("line1_name") or "").strip()
            l2 = str(obj.get("line2_business_name") or "").strip()
            if l1 or l2:
                probe["line1_name"] = l1
                probe["line2_business_name"] = l2
                probe["name_evidence"] = str(obj.get("evidence") or "")[:160]
    if probe:
        probe["page"] = page
    raw.w9_probe = probe


def _merged(raw: RawDoc) -> str:
    return "\n".join(x for x in (raw.raw_text, raw.tesseract_text, raw.vision_text) if x)


def _append_hunt_text(raw: RawDoc, label: str, text: str) -> None:
    """Make a targeted-hunt reply VISIBLE to the extraction model: append it to
    raw_text (Stage B's DOCUMENT TEXT), not only to vision_text/candidates —
    before this fix a hunt's 'Account holder: X' line never reached the model.
    The block must SURVIVE Stage B's raw_text[:STAGE_B_TEXT_LIMIT] cut, so on
    overflow the BASE text is trimmed and the hunt block keeps the tail.
    Type signals (type_hint) are computed from the PRE-hunt text on purpose —
    a hunt reply must never flip the document classification between runs."""
    block = f"\n\n[{label}]\n{text}".rstrip()
    budget = config.STAGE_B_TEXT_LIMIT
    if len(raw.raw_text) + len(block) > budget:
        raw.raw_text = raw.raw_text[:max(0, budget - len(block))]
    raw.raw_text = (raw.raw_text + block).strip()


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
        if ocr.text_layer_garbage(texts):
            # a text layer EXISTS but is unreadable (custom font without a
            # ToUnicode CMap -> control-char soup that still yields enough
            # word-shaped tokens to pass the gate below). Real 5-page vendor
            # packet: every page_score was 0, the first pages won by index,
            # and the extractor was fed garbage. Treat as a scan (P2).
            raw.text_layer_garbage = True
            raw.warnings.append("text layer present but unreadable (encoded/"
                                "garbage font) — treated as a scan")
        if sum(ocr.realword_count(t) for t in texts) >= 15 and not raw.text_layer_garbage:
            # text layer: score every page, keep the most relevant ones,
            # best page first so it never falls off the Stage-B budget
            raw.has_text_layer = True
            raw.survey_texts = dict(enumerate(texts))
            _collect_markers(raw, list(enumerate(texts)), doc_class)
            scored = [(fields.page_score(t, doc_class), i, t, 0) for i, t in enumerate(texts)]
            if _w8_detected(path, texts, doc_class):
                picks, raw.w8_cert_page = _select_w8_pages(scored, max_pages)
            else:
                picks = _select_pages(scored, max_pages)
            raw.pages_used = [i for _, i, _, _ in picks]
            raw.page_texts = {i: t for _, i, t, _ in picks}
            raw.raw_text = "\n".join(t for _, _, t, _ in picks)
        else:
            # scanned: cheap survey of ALL pages (with rotation retry), deep-read the best
            survey = _survey_scanned_pdf(path, render_dir, doc_class)
            raw.survey_texts = {i: t for _, i, t, _ in survey}
            _collect_markers(raw, [(i, t) for _, i, t, _ in survey], doc_class)
            if _w8_detected(path, [t for _, _, t, _ in survey], doc_class):
                picks, raw.w8_cert_page = _select_w8_pages(survey, max_pages)
            else:
                picks = _select_pages(survey, max_pages)
            raw.pages_used = [i for _, i, _, _ in picks]
            _deep_read_pages(path, picks, render_dir, raw, use_vision)
    elif ext in config.IMAGE_EXTS:
        raw.pages = 1
        raw.pages_used = [0]
        _read_image_file(path, render_dir, raw, use_vision)
        raw.survey_texts = {0: raw.tesseract_text}
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

    # type signals are frozen HERE, on the pre-hunt text: (a) probe gates below
    # can rely on them (the invoice guard was dead — type_hint used to be
    # assigned only at the END of perceive), (b) a vision hunt's reply can never
    # flip the document classification between otherwise-identical runs.
    raw.type_hint = fields.type_hint(path.name, raw.raw_text, ext, doc_class)

    # escalation: a bank doc with no account identifiers gets a targeted vision hunt
    if (doc_class == "bank" and use_vision and raw.images
            and not any(k in raw.regex_candidates
                        for k in ("iban", "account_number", "routing_aba"))):
        vt2 = mc.vision("VISION", VISION_TARGETED_PROMPT, raw.images,
                        options={"temperature": 0, "seed": 7})
        if not vt2.startswith("[vision"):
            raw.vision_text = (raw.vision_text + "\n\n[targeted search]\n" + vt2).strip()
            _append_hunt_text(raw, "targeted search", vt2)
            raw.regex_candidates = ocr.regex_fields(_merged(raw))
            raw.warnings.append("targeted vision pass used (no IDs found on first read)")
    # holder hunt: IDs were found but the text carries NO holder-label tokens —
    # the account OWNER is invisible to the text path (weakest-recovery field);
    # one extra vision question while the model is resident. The reply flows
    # through raw text like any read (never blanks anything, leak-gated as
    # ordinary vision text).
    elif (doc_class == "bank" and use_vision and raw.images
            and not _holder_labels_present(_merged(raw))):
        vt3 = mc.vision("VISION", HOLDER_TARGETED_PROMPT, raw.images,
                        options={"temperature": 0, "seed": 7})
        if not vt3.startswith("[vision"):
            raw.vision_text = (raw.vision_text + "\n\n[holder search]\n" + vt3).strip()
            _append_hunt_text(raw, "holder search", vt3)
            raw.regex_candidates = ocr.regex_fields(_merged(raw))
            raw.warnings.append("targeted holder pass used (no holder labels in text)")
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
    # type_hint is computed BEFORE the hunts/probes (see above) — stable input
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
        "w9_pages": raw.w9_pages, "w8_cert_page": raw.w8_cert_page,
        "type_hint": raw.type_hint, "warnings": raw.warnings,
        "text_layer_garbage": raw.text_layer_garbage,
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
