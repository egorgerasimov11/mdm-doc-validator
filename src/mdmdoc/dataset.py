#!/usr/bin/env python3
"""
dataset.py — labeled-example store (dataset/labels.jsonl). One masked labeled
case per line; originals referenced by path+sha256, never copied. Committable:
the leak gate runs on every appended line.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config
from .privacy import assert_no_leak

# serializes the read-rewrite of labels.jsonl across the server threadpool
_LOCK = threading.Lock()


def resolve_doc_path(doc_path: str) -> Path:
    """Label doc_path -> a Path to try. Relative paths live under CORPUS_DIR
    (the portable corpus); an absolute path is honored while it exists (legacy
    labels) and falls back to CORPUS_DIR/<basename>, so a corpus rsynced to
    another machine keeps working without editing labels.jsonl."""
    raw = Path(str(doc_path or ""))
    if not raw.is_absolute():
        return config.CORPUS_DIR / raw
    if raw.exists():
        return raw
    return config.CORPUS_DIR / raw.name


def portable_doc_path(path: str) -> str:
    """Store-form of a document path: relative POSIX when under CORPUS_DIR
    (portable), the original string otherwise."""
    try:
        return Path(str(path)).resolve().relative_to(config.CORPUS_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path or "")


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


def load_synth_labels() -> list[dict]:
    """The synthetic stratum (eval/synthetic/labels.jsonl) — same schema as
    real labels, source:'synthetic'. Deliberately NOT part of load_labels():
    few-shot, LoRA export and precedents must never see synthetic rows."""
    if not config.SYNTH_LABELS_PATH.exists():
        return []
    out = []
    for line in config.SYNTH_LABELS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def all_fakes() -> list[str]:
    out: list[str] = []
    for lab in load_labels():
        out += label_fakes(lab)
    # synthetic identities are fakes by construction — the strict leak sweep
    # must allow them wherever they appear (they can never be real PII)
    for lab in load_synth_labels():
        out += label_fakes(lab)
    return out


def _history_path():
    return config.DATASET_DIR / "labels_history.jsonl"


def append_label(label: dict, secrets: list[str] | None = None) -> None:
    config.ensure_dirs()
    blob = json.dumps(label, ensure_ascii=False)
    assert_no_leak(blob, secrets or [], allowed_fakes=label_fakes(label))
    with _LOCK:
        # replace an earlier label for the same document (latest correction wins);
        # the DISPLACED row is snapshotted to labels_history.jsonl first — it is
        # physically destroyed here otherwise, and undo (F1) needs it back
        keep, displaced = [], []
        for l in load_labels():
            (displaced if l.get("doc_sha256") == label.get("doc_sha256") else keep).append(l)
        if displaced:
            try:
                with open(_history_path(), "a", encoding="utf-8") as f:
                    for old_row in displaced:
                        f.write(json.dumps({**old_row, "_replaced_ts": label.get("ts", ""),
                                            "_replaced_by": label.get("label_id", "")},
                                           ensure_ascii=False) + "\n")
            except Exception:
                pass   # history is best-effort; the save itself must proceed
        lines = [json.dumps(l, ensure_ascii=False) for l in keep] + [blob]
        config.atomic_write_text(config.LABELS_PATH, "\n".join(lines) + "\n")


def delete_label(doc_sha256: str) -> dict | None:
    """Remove the label for one document (undo of label/mark-valid/teach-type).
    Returns the removed row, or None if the sha has no label. The caller decides
    whether to restore a predecessor from labels_history."""
    with _LOCK:
        keep, removed = [], None
        for l in load_labels():
            if l.get("doc_sha256") == doc_sha256 and removed is None:
                removed = l
            else:
                keep.append(l)
        if removed is None:
            return None
        lines = [json.dumps(l, ensure_ascii=False) for l in keep]
        config.atomic_write_text(config.LABELS_PATH,
                                 ("\n".join(lines) + "\n") if lines else "")
    return removed


def last_replaced_label(doc_sha256: str, replaced_ts: str = "") -> dict | None:
    """Newest labels_history row for the sha — the predecessor a label undo
    restores (internal _replaced_* bookkeeping stripped). `replaced_ts` pins the
    displacement moment: only the row displaced BY the label being undone counts,
    an older era must not resurrect."""
    p = _history_path()
    if not p.exists():
        return None
    found = None
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("doc_sha256") != doc_sha256:
            continue
        if replaced_ts and row.get("_replaced_ts") != replaced_ts:
            continue
        found = row
    if found is None:
        return None
    return {k: v for k, v in found.items() if not k.startswith("_replaced")}
