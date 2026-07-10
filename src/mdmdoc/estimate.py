#!/usr/bin/env python3
"""
estimate.py — honest "this will take about N seconds" for the operator.
Rolling mean of recent runs with the same shape; table fallback when history
is thin. Shapes: doc class × (text-layer|scan) × quality × sap.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config


def sniff_text_layer(path: Path) -> bool:
    """Cheap (<1s) guess whether a PDF has a usable text layer."""
    if path.suffix.lower() != ".pdf":
        return False
    try:
        import fitz

        from . import ocr
        doc = fitz.open(path)
        text = "".join(doc[i].get_text() for i in range(min(doc.page_count, 3)))
        doc.close()
        return ocr.realword_count(text) >= 15
    except Exception:
        return False


def shape_key(doc_class: str, text_layer: bool, use_vision: bool,
              sap: bool, quality: bool, effort: int | None = None) -> str:
    """effort joins the key ONLY for non-default levels — effort-3 (and the
    effort-less legacy path) keep byte-identical keys so existing run history
    keeps matching."""
    base = f"{doc_class}|{'text' if text_layer else 'scan'}|" \
           f"{'v' if use_vision else 'nv'}|{'sap' if sap else '-'}|" \
           f"{'q' if quality else '-'}"
    if effort and effort != 3:
        base += f"|e{effort}"
    return base


def _history_mean(key: str, n: int = 5) -> float | None:
    if not config.RUNS_DIR.exists():
        return None
    rows = []
    for meta_p in config.RUNS_DIR.glob("*/meta.json"):
        try:
            m = json.loads(meta_p.read_text())
        except Exception:
            continue
        if m.get("shape_key") == key and m.get("duration_s"):
            rows.append((m.get("ts", ""), float(m["duration_s"])))
    if len(rows) < 2:
        return None
    rows.sort(reverse=True)
    vals = [d for _, d in rows[:n]]
    return sum(vals) / len(vals)


def estimate_seconds(doc_class: str, text_layer: bool, use_vision: bool = True,
                     sap: bool = False, quality: bool = False,
                     effort: int | None = None) -> int:
    if effort:   # the profile decides the strong tier / vision depth (D4)
        quality = quality or effort >= 4
        use_vision = use_vision and effort > 1
    key = shape_key(doc_class, text_layer, use_vision, sap, quality, effort)
    hist = _history_mean(key)
    if hist is not None:
        return int(round(hist / 5) * 5) or 5
    base = config.TIME_BASE_S["text_layer" if text_layer else "scan"]
    if use_vision:
        base += config.TIME_MODIFIERS_S["signature"]
    if quality:
        base += config.TIME_MODIFIERS_S["strong"]
    if sap:
        base += config.TIME_MODIFIERS_S["sap"]
    if effort == 5:
        base += config.TIME_MODIFIERS_S["strong"]   # thinking + extra probes
    if effort == 1:
        base = max(5, base - config.TIME_MODIFIERS_S["signature"])
    return base


def human(seconds: int) -> str:
    return f"~{seconds}s" if seconds < 100 else f"~{seconds / 60:.1f} min"
