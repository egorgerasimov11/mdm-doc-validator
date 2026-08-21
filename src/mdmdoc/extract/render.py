"""Page rendering with an on-disk cache — shared by the benchmark and the extractor.

A render is identified by (source file, page index, preset, rotation). The
rotation of a page is decided ONCE (tesseract OSD with a brute-force fallback,
via stage_a._best_rotation) and remembered in <cache_dir>/meta.json so every
preset of that page shares it.

Presets (see PRESETS):
  v170   170 dpi RGB, max side 1600, JPEG  — today's production vision render
  v200   200 dpi RGB, max side 2000, JPEG
  v300   300 dpi RGB, no cap, JPEG
  g300   300 dpi grayscale + autocontrast, PNG — tesseract / Apple Vision
  q120   120 dpi grayscale PNG, no rotation  — survey & OSD
  gold   150 dpi RGB PNG                    — the full-page image shown to Claude
  gold300 300 dpi RGB PNG                   — the page the gold tiles are cut from
  ac     like v200 + photo fix (EXIF transpose, autocontrast)

Model renders are JPEG on purpose: model_client._load_image_capped downsizes
any image over 1.5 MB, so a 2000-px PNG would be silently shrunk.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageOps

from .. import config, ocr

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


@dataclass(frozen=True)
class RenderSpec:
    name: str
    dpi: int
    max_side: int | None
    mode: str = "rgb"          # "rgb" | "gray"
    rotation: str = "auto"     # "auto" (detect once, cached) | "none"
    fmt: str = "jpg"           # "jpg" | "png"
    photo_fix: bool = False    # EXIF transpose + autocontrast (photos)
    quality: int = 90          # JPEG quality

    @property
    def ext(self) -> str:
        return ".jpg" if self.fmt == "jpg" else ".png"


PRESETS: dict[str, RenderSpec] = {
    "v170": RenderSpec("v170", 170, 1600, "rgb", "auto", "jpg"),
    "v200": RenderSpec("v200", 200, 2000, "rgb", "auto", "jpg"),
    "v300": RenderSpec("v300", 300, None, "rgb", "auto", "jpg"),
    "g300": RenderSpec("g300", 300, None, "gray", "auto", "png"),
    "q120": RenderSpec("q120", 120, None, "gray", "none", "png"),
    "gold": RenderSpec("gold", 150, None, "rgb", "auto", "png"),
    "gold300": RenderSpec("gold300", 300, None, "rgb", "auto", "png"),
    "ac": RenderSpec("ac", 200, 2000, "rgb", "auto", "jpg", photo_fix=True),
}


def preset(name: str) -> RenderSpec:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown render preset {name!r}; known: {', '.join(PRESETS)}") from None


# ── source access ─────────────────────────────────────────────────────────────

def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def page_count(path: Path) -> int:
    if is_image(path):
        return 1
    try:
        with fitz.open(path) as d:
            return d.page_count
    except Exception:
        return 0


def _load_page_image(src: Path, idx: int, dpi: int) -> Image.Image:
    """Raw page pixels: PDF page at `dpi`, or the image file as-is (EXIF-transposed)."""
    if is_image(src):
        im = Image.open(src)
        im = ImageOps.exif_transpose(im)
        return im.convert("RGB")
    with fitz.open(src) as d:
        pix = d[idx].get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


# ── rotation (once per page, cached) ──────────────────────────────────────────

def _meta_path(cache_dir: Path) -> Path:
    return cache_dir / "meta.json"


def _load_meta(cache_dir: Path) -> dict:
    p = _meta_path(cache_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_meta(cache_dir: Path, meta: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    config.atomic_write_text(_meta_path(cache_dir), json.dumps(meta, ensure_ascii=False, indent=1))


def page_rotation(src: Path, cache_dir: Path, idx: int) -> int:
    """Detected rotation (0/90/180/270) for a page, cached in meta.json.
    Uses the survey render (q120) + tesseract OSD / brute force; 0 without tesseract."""
    meta = _load_meta(cache_dir)
    rots = meta.setdefault("rotation", {})
    key = str(idx)
    if key in rots:
        return int(rots[key])
    rot = 0
    if ocr.HAVE_TESSERACT:
        try:
            from ..stage_a import _best_rotation, _quick_ocr
            q = render_page(src, cache_dir, idx, PRESETS["q120"])
            text = _quick_ocr(str(q), force_cjk=True)
            rot, _ = _best_rotation(str(q), text, force_cjk=True)
        except Exception:
            rot = 0
    rots[key] = int(rot)
    meta["rotation"] = rots
    _save_meta(cache_dir, meta)
    return int(rot)


def set_page_rotation(cache_dir: Path, idx: int, rot: int) -> None:
    """Manual override (manifest `rotation` tag or operator correction)."""
    meta = _load_meta(cache_dir)
    meta.setdefault("rotation", {})[str(idx)] = int(rot)
    _save_meta(cache_dir, meta)


# ── rendering ─────────────────────────────────────────────────────────────────

def render_name(idx: int, spec: RenderSpec, rot: int) -> str:
    return f"p{idx}_{spec.name}_r{rot}{spec.ext}"


def render_page(src: Path, cache_dir: Path, idx: int, spec: RenderSpec) -> Path:
    """Render one page under `spec`, reusing the cached file when present."""
    rot = page_rotation(src, cache_dir, idx) if spec.rotation == "auto" else 0
    out = cache_dir / render_name(idx, spec, rot)
    if out.exists() and out.stat().st_size > 0:
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    im = _load_page_image(src, idx, spec.dpi)
    if spec.photo_fix:
        im = ImageOps.autocontrast(im, cutoff=1)
    if rot:
        im = im.rotate(-rot, expand=True)
    if spec.mode == "gray":
        im = ocr._preprocess(im)          # grayscale → upscale small → autocontrast
    else:
        im = im.convert("RGB")
    if spec.max_side and max(im.size) > spec.max_side:
        f = spec.max_side / max(im.size)
        im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))), Image.LANCZOS)
    tmp = out.with_name(out.name + ".tmp" + spec.ext)
    if spec.fmt == "jpg":
        im.convert("RGB").save(tmp, "JPEG", quality=spec.quality, optimize=True)
    else:
        im.save(tmp, "PNG")
    os.replace(tmp, out)
    return out


# ── tiles ─────────────────────────────────────────────────────────────────────

TILE_LAYOUTS = {
    # name: list of (x0, y0, x1, y1) in relative page coordinates, reading order
    "h2": [(0.0, 0.0, 1.0, 0.54), (0.0, 0.46, 1.0, 1.0)],
    "q4": [(0.0, 0.0, 0.53, 0.53), (0.47, 0.0, 1.0, 0.53),
           (0.0, 0.47, 0.53, 1.0), (0.47, 0.47, 1.0, 1.0)],
    "r3x2": [(0.0, 0.0, 0.53, 0.36), (0.47, 0.0, 1.0, 0.36),
             (0.0, 0.32, 0.53, 0.68), (0.47, 0.32, 1.0, 0.68),
             (0.0, 0.64, 0.53, 1.0), (0.47, 0.64, 1.0, 1.0)],
}


def tiles(page_img: Path, layout: str, min_long_side: int = 1100) -> list[Path]:
    """Cut a rendered page into overlapping tiles (cached next to the page).
    Small crops read far better than full pages for both OCR and vision."""
    boxes = TILE_LAYOUTS[layout]
    out: list[Path] = []
    im = None
    for k, (x0, y0, x1, y1) in enumerate(boxes):
        p = page_img.with_name(f"{page_img.stem}_t{layout}{k}{page_img.suffix}")
        out.append(p)
        if p.exists() and p.stat().st_size > 0:
            continue
        if im is None:
            im = Image.open(page_img)
        w, h = im.size
        crop = im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
        if max(crop.size) < min_long_side:
            f = min_long_side / max(crop.size)
            crop = crop.resize((int(crop.width * f), int(crop.height * f)), Image.LANCZOS)
        tmp = p.with_name(p.name + ".tmp" + p.suffix)
        if p.suffix.lower() in (".jpg", ".jpeg"):
            crop.convert("RGB").save(tmp, "JPEG", quality=90, optimize=True)
        else:
            crop.save(tmp, "PNG")
        os.replace(tmp, p)
    return out


def prune(cache_dir: Path, spec_name: str) -> int:
    """Delete every cached render of one preset (disk housekeeping)."""
    n = 0
    for p in cache_dir.glob(f"p*_{spec_name}_r*"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def with_name(spec: RenderSpec, **changes) -> RenderSpec:
    """Derive a variant preset (e.g. a custom max_side) keeping cache names distinct."""
    s = replace(spec, **changes)
    if "name" not in changes:
        s = replace(s, name=f"{spec.name}-" + "-".join(f"{k}{v}" for k, v in sorted(changes.items())))
    return s
