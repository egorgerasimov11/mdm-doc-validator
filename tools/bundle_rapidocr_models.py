#!/usr/bin/env python3
"""Collect the RapidOCR model files the offline extractor needs into
<project>/models/rapidocr/ (gitignored), so a packaged module never downloads.

Sources, in order: an existing bundle named by MDMDOC_RAPIDOCR_MODELS, the
installed rapidocr package's own models folder, and — only when a file is
still missing — RapidOCR's own downloader (needs network once, on THIS machine).

    uv run --group bench python tools/bundle_rapidocr_models.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdmdoc.extract.engines import RAPIDOCR_MODEL_FILES  # noqa: E402


def main() -> int:
    dest = ROOT / "models" / "rapidocr"
    dest.mkdir(parents=True, exist_ok=True)
    import rapidocr
    pkg_models = Path(rapidocr.__file__).parent / "models"
    sources = [Path(p) for p in (os.environ.get("MDMDOC_RAPIDOCR_MODELS"),) if p] + [pkg_models]
    missing = []
    for name in RAPIDOCR_MODEL_FILES:
        if (dest / name).exists():
            continue
        src = next((s / name for s in sources if (s / name).exists()), None)
        if src is None:
            missing.append(name)
            continue
        shutil.copy2(src, dest / name)
        print(f"copied {name} from {src.parent}")
    if missing:
        # ask RapidOCR to fetch what is missing, then copy again
        print(f"downloading via rapidocr: {', '.join(missing)}")
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion
        for lang in ("latin", "ch"):
            params = {"Rec.lang_type": LangRec(lang)}
            if lang != "ch":
                params["Rec.ocr_version"] = OCRVersion("PP-OCRv5")
                params["Rec.model_type"] = ModelType("mobile")
            RapidOCR(params=params)
        for name in list(missing):
            if (pkg_models / name).exists():
                shutil.copy2(pkg_models / name, dest / name)
                print(f"copied {name} after download")
                missing.remove(name)
    for name in RAPIDOCR_MODEL_FILES:
        p = dest / name
        print(f"{'ok ' if p.exists() else 'MISSING'} {name} {p.stat().st_size // 1024 if p.exists() else 0} KB")
    (dest / "README.md").write_text(
        "RapidOCR model files bundled for the offline Documents module.\n"
        "Source: the rapidocr PyPI package / its model hub (PP-OCR, Apache-2.0).\n"
        "Rebuild with: uv run --group bench python tools/bundle_rapidocr_models.py\n", encoding="utf-8")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
