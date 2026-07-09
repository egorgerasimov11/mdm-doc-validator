#!/usr/bin/env python3
"""
ocr.py — high-fidelity OCR + deterministic field regex for scanned documents.
Vendored from form-validator (agents/ocr.py) with one fix: realword_count()
is defined here (upstream referenced it without importing from common.py).

Renders PDF pages / images at 300 DPI with grayscale + autocontrast, runs
tesseract for an accurate text layer, and pulls the well-structured IDs
(IBAN / SWIFT / EIN / SSN / routing / account) with regex — so critical IDs
come from literal OCR, not a model guess.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageOps

HAVE_TESSERACT = bool(shutil.which("tesseract"))
RENDER_DPI = 300

_WORD = re.compile(r"[A-Za-zÀ-ÿ]{2,}")


def realword_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def _avail_langs() -> set:
    if not HAVE_TESSERACT:
        return set()
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=10)
        return set(r.stdout.decode("utf-8", "replace").split())
    except Exception:
        return set()


_LANGS = _avail_langs()


def cjk_lang() -> str:
    have = [l for l in ("eng", "kor", "jpn", "chi_sim", "chi_tra") if l in _LANGS]
    return "+".join(have) or "eng"


def _safe_stem(name: str) -> str:
    # tesseract/leptonica choke on spaces in paths -> sanitize render filenames
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "doc"


def is_locked_pdf(path: Path) -> bool:
    try:
        d = fitz.open(path)
        locked = bool(d.needs_pass)
        d.close()
        return locked
    except Exception:
        return False


def _preprocess(im: Image.Image) -> Image.Image:
    im = im.convert("L")               # grayscale
    if min(im.size) < 1400:            # upscale small/low-res scans
        f = 1600 / min(im.size)
        im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
    return ImageOps.autocontrast(im)


def render_pdf_pages(pdf_path: Path, out_dir: Path, max_pages: int = 2) -> list[str]:
    out: list[str] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return out
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        try:
            pix = page.get_pixmap(dpi=RENDER_DPI)
            png = out_dir / f"{_safe_stem(pdf_path.stem)}.p{i}.png"
            pix.save(png)
            _preprocess(Image.open(png)).save(png)
            out.append(str(png))
        except Exception:
            continue
    doc.close()
    return out


def prepare_image(img_path: Path, out_dir: Path) -> str:
    try:
        out = out_dir / f"{_safe_stem(Path(img_path).stem)}.proc.png"
        _preprocess(Image.open(img_path)).save(out)
        return str(out)
    except Exception:
        return str(img_path)


def tesseract_text(png_path: str, lang: str = "eng") -> str:
    if not HAVE_TESSERACT:
        return ""
    p = Path(png_path)
    try:
        # run with cwd=image dir + basename arg: leptonica resolves relative to CWD even
        # where absolute-path fopen is restricted (sandboxed subprocesses).
        r = subprocess.run(["tesseract", p.name, "-", "-l", lang, "--psm", "3"],
                           capture_output=True, timeout=90, cwd=str(p.parent))
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


# --- deterministic field regex on OCR text --------------------------------
def _clean(s: str) -> str:
    return re.sub(r"[\s]", "", s or "").upper()


IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){10,30})\b")
SWIFT_RE = re.compile(r"\b([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")
# (?<![\d-]) / (?![\d-]) guards: \b alone lets these match INSIDE hyphen-grouped
# bank numbers (LV/GB sort-code prints like "61-26-1234500" yielded a phantom
# EIN "26-1234500" — a fake TIN 'secret' that then gate-blocked the document's
# own account digits). A real EIN/SSN is never digit- or hyphen-adjacent.
EIN_RE = re.compile(r"(?<![\d-])(\d{2}-\d{7})(?![\d-])")
SSN_RE = re.compile(r"(?<![\d-])(\d{3}-\d{2}-\d{4})(?![\d-])")


def regex_fields(text: str) -> dict:
    t = text or ""
    out: dict = {}
    m = IBAN_RE.search(t)
    if m:
        iban = _clean(m.group(1))
        if 15 <= len(iban) <= 34:
            out["iban"] = iban
    # SWIFT: pick a candidate that is not a common word run; prefer near 'BIC/SWIFT'
    sw = None
    near = re.search(r"(?i)(?:bic|swift)[^A-Z0-9]{0,8}([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)", t)
    if near:
        sw = near.group(1).upper()
    else:
        for c in SWIFT_RE.findall(t):
            if not c.isalpha():            # require at least one digit -> real BIC, not a word
                sw = c.upper()
                break
    if sw:
        out["swift_bic"] = sw
    m = EIN_RE.search(t)
    if m:
        out["ein"] = m.group(1)
    else:
        # scanned W-9 boxes flatten to separated digits near the EIN label:
        # "Employer identification number ... 8 1 – 0 8 2 6 7 3 4"
        m = re.search(r"(?is)employer\s+identification\s+number.{0,120}?"
                      r"((?:\d[ \t]*[\-–—]?[ \t]*){8}\d)", t)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) == 9:
                out["ein"] = digits[:2] + "-" + digits[2:]
    m = SSN_RE.search(t)
    if m:                                  # mask immediately — never store full SSN
        s = m.group(1)
        out["ssn_masked"] = "***-**-" + s[-4:]
    # routing/ABA: 9 digits near a routing keyword. US bank letters often carry
    # TWO values (standard/ACH vs domestic wires) under labels like
    # "Bank ABA (standard):" — wide label->digits window, ALL matches kept,
    # qualifier words route them into separate fields.
    seen: list = []
    for rm in re.finditer(r"(?i)((?:bank\s+)?(?:routing|aba|ach|rtn|wire[s]?)"
                          r"(?:[ /]*(?:aba|routing|number|no\.?|#))?"
                          r"[^0-9]{0,28})(\d{9})\b", t):
        label, val = rm.group(1).lower(), rm.group(2)
        if val in seen:
            continue
        seen.append(val)
        if any(w in label for w in ("wire", "domestic")):
            out.setdefault("routing_aba_wires", val)
        else:
            out.setdefault("routing_aba", val)
    # a wires-qualified value with no standard one: keep field semantics honest
    if "routing_aba" not in out and "routing_aba_wires" in out and len(seen) == 1:
        pass  # single wires ABA stays in routing_aba_wires only
    # account number: digits near an 'account' keyword in EN/ES/DE/FR/PT/RU/CJK
    for am in re.finditer(r"(?i)(?:acc(?:oun)?t|cuenta|cta\.?|konto(?:nummer)?|kto\.?|"
                          r"compte|conta|сч[её]т|계좌|口座|账[户戶号號]|帐[户戶号號])"
                          r"[^0-9]{0,14}([0-9][0-9 \-]{5,20}[0-9])", t):
        acct = re.sub(r"[\s\-]", "", am.group(1))
        if 6 <= len(acct) <= 18 and acct != out.get("routing_aba"):
            out["account_number"] = acct
            break
    # fallback: a long standalone digit run (11-18 digits) as account candidate
    if "account_number" not in out:
        cands = [re.sub(r"[\s\-]", "", m) for m in re.findall(r"\b\d[\d \-]{9,20}\d\b", t)]
        cands = [c for c in cands if 11 <= len(c) <= 18 and c != out.get("routing_aba")]
        if cands:
            out["account_number"] = max(cands, key=len)
    return out


def read_document(path: Path, render_dir: Path, max_pages: int = 2) -> dict:
    """Return {ocr_text, regex, images, locked} for a scanned doc (PDF or image).
    Runs tesseract in English first, then retries with CJK languages if the page
    looks non-Latin."""
    ext = path.suffix.lower()
    if ext == ".pdf" and is_locked_pdf(path):
        return {"ocr_text": "", "regex": {}, "images": [], "locked": True}
    images: list[str] = []
    if ext == ".pdf":
        images = render_pdf_pages(path, render_dir, max_pages=max_pages)
    else:
        images = [prepare_image(path, render_dir)]
    has_cjk = any(l in _LANGS for l in ("kor", "jpn", "chi_sim"))
    parts = []
    for im in images:
        t = tesseract_text(im, "eng")
        if has_cjk and realword_count(t) < 8:           # likely non-Latin -> CJK retry
            t2 = tesseract_text(im, cjk_lang())
            if len(t2.strip()) > len(t.strip()):
                t = t2
        parts.append(t)
    ocr = "\n".join(parts).strip()
    return {"ocr_text": ocr, "regex": regex_fields(ocr), "images": images, "locked": False}
