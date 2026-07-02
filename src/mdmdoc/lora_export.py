#!/usr/bin/env python3
"""
lora_export.py — export the label dataset in mlx-lm chat format for a future
LoRA fine-tune of the Stage-B text model (see TRAINING.md). All sensitive
values are shape-preserving fakes; the leak gate runs on every line.

Gated below --min-labels (default 100): a LoRA on a handful of examples
overfits and silently degrades — collect labels first.
"""
from __future__ import annotations

import json

from . import config
from .dataset import load_labels
from .fewshot import label_to_example
from .privacy import assert_no_leak


def _system(doc_class: str) -> str:
    p = config.PROMPTS_DIR / f"system_{'bank' if doc_class == 'bank' else 'w9'}.txt"
    return p.read_text() if p.exists() else ""


def export_lora(min_labels: int = 100, force: bool = False, split: float = 0.85) -> int:
    labels = [l for l in load_labels() if l.get("confirmed")]
    if len(labels) < min_labels and not force:
        print(f"only {len(labels)} labels — LoRA export is gated at {min_labels} "
              "(a tiny dataset overfits). Keep labeling with `mdmdoc review`, or pass --force "
              "to export anyway (e.g. to validate the format).")
        return 1
    config.ensure_dirs()
    # stratified split by (doc_class, doc_type)
    groups: dict = {}
    for lab in labels:
        groups.setdefault((lab.get("doc_class"), lab.get("doc_type_gold")), []).append(lab)
    train, valid = [], []
    for _, labs in sorted(groups.items()):
        cut = max(1, int(len(labs) * split)) if len(labs) > 1 else 1
        train += labs[:cut]
        valid += labs[cut:]
    if not valid and train:
        valid.append(train.pop())

    def to_chat(lab: dict) -> str:
        ex = label_to_example(lab)
        msg = {"messages": [
            {"role": "system", "content": _system(lab["doc_class"])},
            {"role": "user", "content": ex["input"]},
            {"role": "assistant", "content": json.dumps(ex["output"], ensure_ascii=False)},
        ]}
        line = json.dumps(msg, ensure_ascii=False)
        from .dataset import label_fakes
        assert_no_leak(line, allowed_fakes=label_fakes(lab))
        return line

    for name, labs in (("train", train), ("valid", valid)):
        p = config.LORA_DIR / f"{name}.jsonl"
        p.write_text("\n".join(to_chat(l) for l in labs) + "\n", encoding="utf-8")
        print(f"{name}: {len(labs)} examples -> {p}")
    print("Next: follow TRAINING.md (mlx_lm.lora -> fuse -> GGUF -> ollama create).")
    return 0
