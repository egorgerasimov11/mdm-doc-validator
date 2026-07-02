#!/usr/bin/env python3
"""
dataset.py — labeled-example store (dataset/labels.jsonl). One masked labeled
case per line; originals referenced by path+sha256, never copied. Committable:
the leak gate runs on every appended line.
"""
from __future__ import annotations

import json

from . import config
from .privacy import assert_no_leak


def load_labels() -> list[dict]:
    if not config.LABELS_PATH.exists():
        return []
    out = []
    for line in config.LABELS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def count_labels() -> int:
    return len(load_labels())


def label_fakes(label: dict) -> list[str]:
    return [it.get("fake", "") for it in label.get("sensitive_map", []) if it.get("fake")]


def all_fakes() -> list[str]:
    out: list[str] = []
    for lab in load_labels():
        out += label_fakes(lab)
    return out


def append_label(label: dict, secrets: list[str] | None = None) -> None:
    config.ensure_dirs()
    blob = json.dumps(label, ensure_ascii=False)
    assert_no_leak(blob, secrets or [], allowed_fakes=label_fakes(label))
    # replace an earlier label for the same document (latest correction wins)
    existing = [l for l in load_labels() if l.get("doc_sha256") != label.get("doc_sha256")]
    lines = [json.dumps(l, ensure_ascii=False) for l in existing] + [blob]
    config.LABELS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
