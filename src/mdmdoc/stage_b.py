#!/usr/bin/env python3
"""
stage_b.py — structured extraction (THE trainable stage): raw text -> JSON
{doc_type, fields}. Runs a small local text model with an editable system prompt
plus generated few-shot exemplars (prompts/fewshot/*.json, fake-shape values).

The model classifies and extracts ONLY — verdicts come from the rule engine.
The prompt contains full sensitive values and is therefore NEVER persisted.
"""
from __future__ import annotations

import json

from . import config, model_client as mc
from .fields import (BANK_DOC_TYPES, BANK_KEYS, W9_DOC_TYPES, W9_KEYS, Extraction,
                     crosscheck_ids)
from .stage_a import RawDoc


def _load_system(doc_class: str) -> str:
    p = config.PROMPTS_DIR / f"system_{'bank' if doc_class == 'bank' else 'w9'}.txt"
    return p.read_text() if p.exists() else ""


def _load_fewshot(doc_class: str) -> list[dict]:
    p = config.FEWSHOT_DIR / f"{'bank' if doc_class == 'bank' else 'w9'}.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def build_prompt(raw: RawDoc) -> str:
    doc_class = raw.doc_class
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    parts = []
    for ex in _load_fewshot(doc_class):
        parts.append("EXAMPLE INPUT:\n" + ex.get("input", "")
                     + "\nEXAMPLE OUTPUT:\n" + json.dumps(ex.get("output", {}), ensure_ascii=False))
    cand = {k: v for k, v in raw.regex_candidates.items()}
    parts.append(
        "DOCUMENT FILENAME: " + raw.path.rsplit("/", 1)[-1]
        + ("\nHEURISTIC TYPE HINT: " + raw.type_hint if raw.type_hint else "")
        + "\nOCR-VERIFIED CANDIDATES (from deterministic regex — trust these over your own reading):\n"
        + json.dumps(cand, ensure_ascii=False)
        + "\n\nDOCUMENT TEXT:\n" + raw.raw_text[:config.STAGE_B_TEXT_LIMIT]
        + "\n\nReturn JSON with exactly these keys: doc_type (one of "
        + ", ".join(types) + "), fields {" + ", ".join(keys) + "}. "
        + "Use \"\" for absent string fields, false for absent boolean flags. Do not invent values."
    )
    return "\n\n".join(parts)


def extract(raw: RawDoc) -> Extraction:
    doc_class = raw.doc_class
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    types = BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES
    ext_res = Extraction(doc_class=doc_class, model_id=mc.resolve("TEXT"))
    ext_res.warnings = list(raw.warnings)

    # deterministic overrides that need no model
    if raw.editable:
        ext_res.doc_type = "editable_source"
    elif raw.ext in config.EMAIL_EXTS:
        ext_res.doc_type = "email"

    if raw.raw_text.strip():
        # 16k ctx: system + few-shot exemplars + 8k doc text must never truncate.
        # temperature 0 + fixed seed: extraction must be reproducible, otherwise
        # eval before/after deltas drown in run-to-run jitter.
        obj, first_try = mc.generate_json("TEXT", build_prompt(raw), system=_load_system(doc_class),
                                          options={"num_ctx": 16384, "temperature": 0, "seed": 7})
        mc.unload("TEXT")
        ext_res.json_valid_first_try = first_try
        if isinstance(obj, dict):
            model_type = str(obj.get("doc_type", "") or "").strip().lower()
            if not ext_res.doc_type:  # deterministic overrides win
                ext_res.doc_type = model_type if model_type in types else (raw.type_hint or "other")
            flds = obj.get("fields") if isinstance(obj.get("fields"), dict) else obj
            ext_res.fields = {k: flds.get(k, "") for k in keys}
        else:
            ext_res.warnings.append("stage-B model returned no valid JSON")
            ext_res.doc_type = ext_res.doc_type or raw.type_hint or ("other" if doc_class == "bank" else "unknown")
            ext_res.fields = {k: "" for k in keys}
    else:
        ext_res.doc_type = ext_res.doc_type or raw.type_hint or ("other" if doc_class == "bank" else "unknown")
        ext_res.fields = {k: "" for k in keys}
        if not raw.locked and not raw.editable:
            ext_res.warnings.append("no text available for extraction")

    # deterministic type hints beat a hesitant model on hard-reject types
    if raw.type_hint == "invoice" and ext_res.doc_type not in ("invoice",):
        ext_res.warnings.append(f"type hint 'invoice' overrides model '{ext_res.doc_type}'")
        ext_res.doc_type = "invoice"
    if doc_class == "w9" and raw.type_hint == "w8" and ext_res.doc_type == "w9":
        ext_res.warnings.append("type hint 'w8' overrides model 'w9'")
        ext_res.doc_type = "w8"

    # normalize tin_type to the two canonical values (model may echo box captions)
    if doc_class == "w9":
        tt = str(ext_res.fields.get("tin_type") or "").lower()
        ext_res.fields["tin_type"] = ("SSN" if "ssn" in tt or "social" in tt
                                      else "EIN" if "ein" in tt or "employer" in tt else "")

    ext_res.crosscheck = crosscheck_ids(ext_res.fields, raw.regex_candidates, doc_class)
    ext_res.register_secrets()
    # regex candidates hold full values too — register so the leak gate knows them
    for k, v in raw.regex_candidates.items():
        if k in ("iban", "account_number", "routing_aba", "ein"):
            from .privacy import FIELD_KIND
            ext_res.vault.register(FIELD_KIND.get(k, "account_number"), v)
    return ext_res
