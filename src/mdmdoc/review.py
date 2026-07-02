#!/usr/bin/env python3
"""
review.py — the teaching half of the loop: show what the model extracted, let
Egor correct it, store a masked labeled example. Full sensitive values typed
during review exist only in memory; the label stores masked + derived facts +
shape-preserving fakes for few-shot/LoRA use.
"""
from __future__ import annotations

import re
import subprocess

from . import config, runstore
from .dataset import append_label
from .fields import (BANK_DOC_TYPES, BANK_KEYS, W9_DOC_TYPES, W9_KEYS, Extraction)
from .privacy import FIELD_KIND, SecretVault, mask
from .rules.engine import VERDICTS

_SENSITIVE = {"iban", "account_number", "routing_aba", "tin_raw"}
_BOOLEAN = {"signed", "partial_capture"}


def _display_value(key: str, pub_fields: dict) -> str:
    v = pub_fields.get("tin" if key == "tin_raw" else key, "")
    if isinstance(v, dict):
        return v.get("masked", "") if v.get("present") else ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v or "")


def _ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt).strip()
    except EOFError:
        return default
    return s if s else default


def review_run(spec: str, open_doc: bool = False) -> int:
    run_id = runstore.resolve_run(spec)
    if not run_id:
        print(f"run not found: {spec} (try `mdmdoc runs`)")
        return 1
    meta = runstore.load(run_id, "meta.json") or {}
    pub = runstore.load(run_id, "extraction.json") or {}
    stage_a_pub = runstore.load(run_id, "stage_a.json") or {}
    rep = runstore.load(run_id, "report.json") or {}
    if isinstance(rep, str):
        import json
        rep = json.loads(rep)
    doc_class = meta.get("doc_class", "bank")
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES

    print(f"\nReviewing run {run_id} — {meta.get('file_name')} ({doc_class})")
    if open_doc and meta.get("path"):
        subprocess.run(["open", meta["path"]], check=False)

    vault = SecretVault()
    gold_fields: dict = {}
    model_diff: dict = {}
    smap_extra: list = []   # fakes for kept-as-is sensitive fields (no real value typed)
    pub_fields = pub.get("fields", {})

    def _keep_sensitive(k: str, entry: dict) -> None:
        """User kept the model's value: reuse the run's public entry as gold and
        synthesize a consistent fake (we never saw the real value here)."""
        from .privacy import fake_preserve_shape
        gk = "tin" if k == "tin_raw" else k
        gold_fields[gk] = dict(entry)
        if not entry.get("present"):
            return
        kind = FIELD_KIND.get(k, "account_number")
        n = int(entry.get("digits") or entry.get("length") or 9)
        if k == "iban":
            template = (entry.get("country") or "XX") + "0" * max(n - 2, 4)
        elif kind == "tin" and entry.get("hyphenated"):
            template = ("000-00-0000" if (entry.get("type") == "SSN")
                        else "00-0000000")
        else:
            template = "0" * n
        smap_extra.append({"kind": kind, "masked": entry.get("masked", ""),
                           "fake": fake_preserve_shape(kind, template, seed=run_id + k)})

    print("For each field: Enter = keep the model's value, or type the correction "
          "('-' clears the field).")
    for k in keys:
        if k == "tin_type":
            continue  # asked together with tin_raw below
        shown = _display_value(k, pub_fields)
        if k in _BOOLEAN:
            ans = _ask(f"  {k:24} [{shown or 'no'}] (y/n): ")
            val = shown == "yes" if not ans else ans.lower().startswith("y")
            gold_fields[k] = bool(val)
            continue
        ans = _ask(f"  {k:24} [{shown}]: ")
        val = shown if not ans else ("" if ans == "-" else ans)
        if k in _SENSITIVE and not ans:
            pk = "tin" if k == "tin_raw" else k
            entry = pub_fields.get(pk)
            _keep_sensitive(k, entry if isinstance(entry, dict) else {"present": False, "masked": ""})
            continue
        if k in _SENSITIVE and val:
            kind = FIELD_KIND.get(k, "account_number")
            vault.register(kind, val)
            if k == "tin_raw":
                digits = re.sub(r"\D", "", val)
                tt = _ask(f"  {'tin_type':24} [{(pub_fields.get('tin') or {}).get('type', '')}]: ",
                          (pub_fields.get("tin") or {}).get("type", ""))
                gold_fields["tin"] = {"type": tt.upper(), "masked": mask("tin", val),
                                      "digits": len(digits), "hyphenated": "-" in val,
                                      "present": True}
            else:
                from .fields import _norm_id
                c = _norm_id(val)
                entry = {"masked": mask(kind, val), "length": len(c), "present": True}
                if k == "iban":
                    entry["country"] = c[:2] if c[:2].isalpha() else ""
                gold_fields[k] = entry
        elif k in _SENSITIVE:
            gold_fields[k if k != "tin_raw" else "tin"] = {"present": False, "masked": ""}
        else:
            gold_fields[k] = val
        if val != shown and not (k in _SENSITIVE and not ans):
            model_diff[k] = {"model": shown, "gold": mask(FIELD_KIND[k], val) if k in _SENSITIVE and val else val}

    print(f"\nDoc types: {', '.join(types)}")
    doc_type_gold = _ask(f"doc_type [{pub.get('doc_type', '')}]: ", pub.get("doc_type", ""))
    print(f"Verdicts: {', '.join(VERDICTS)}")
    verdict_gold = _ask(f"verdict [{rep.get('verdict', '')}]: ", rep.get("verdict", ""))
    notes = _ask("notes (optional): ")

    label = {
        "label_id": f"lbl-{runstore.now_iso().replace(':', '').replace('-', '')}",
        "ts": runstore.now_iso(),
        "doc_sha256": run_id,
        "doc_path": meta.get("path"),
        "doc_class": doc_class,
        "doc_type_gold": doc_type_gold,
        "fields_gold": gold_fields,
        "verdict_gold": verdict_gold,
        "stage_a_excerpt": (stage_a_pub.get("raw_text_excerpt") or "")[:config.EXCERPT_LIMIT],
        "regex_candidates_masked": stage_a_pub.get("regex_candidates_masked", {}),
        "model_predicted": {"doc_type": pub.get("doc_type"), "fields_diff": model_diff},
        "sensitive_map": vault.sensitive_map() + smap_extra,
        "reviewer": "egor",
        "notes": notes,
        "confirmed": True,
    }
    append_label(label, vault.secrets())
    from .dataset import count_labels
    print(f"\nSaved label for {meta.get('file_name')} — dataset now has {count_labels()} examples.")
    print("Run `mdmdoc train --fewshot` to fold corrections into the prompts, "
          "then `mdmdoc eval` to measure the effect.")
    return 0
