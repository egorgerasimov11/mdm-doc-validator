#!/usr/bin/env python3
"""
review_core.py — programmatic review: the decision logic behind labeling,
shared by the stdin CLI (review.py) and the HTTP API/web UI.

A submission is:
    {"fields": {key: {"action": "keep"|"set"|"clear", "value": str|bool|None}},
     "doc_type_gold": str, "verdict_gold": str, "notes": str, "reviewer": str}

Sensitive fields may carry FULL values in a "set" — they live in memory only;
the stored label holds masked values + derived facts + shape-preserving fakes
(dataset.append_label re-runs the leak gate on the serialized line).
"""
from __future__ import annotations

import re

from . import config, runstore
from .dataset import append_label, count_labels
from .fields import BANK_DOC_TYPES, BANK_KEYS, W9_DOC_TYPES, W9_KEYS, _norm_id
from .privacy import FIELD_KIND, SecretVault, fake_preserve_shape, mask
from .rules.engine import VERDICTS

SENSITIVE_KEYS = {"iban", "account_number", "routing_aba", "routing_aba_wires", "tin_raw"}
BOOLEAN_KEYS = {"signed", "partial_capture"}
_BANK_PUB_KEYS = ("iban", "account_number", "routing_aba", "routing_aba_wires")


class RunNotFound(KeyError):
    pass


def _load_run(run_id: str) -> tuple[dict, dict, dict, dict]:
    meta = runstore.load(run_id, "meta.json")
    if not meta:
        raise RunNotFound(run_id)
    pub = runstore.load(run_id, "extraction.json") or {}
    stage_a_pub = runstore.load(run_id, "stage_a.json") or {}
    rep = runstore.load(run_id, "report.json") or {}
    if isinstance(rep, str):
        import json
        rep = json.loads(rep)
    return meta, pub, stage_a_pub, rep


def _masked_shown(key: str, pub_fields: dict, shown: str) -> str:
    """Masked form of what the form displayed (labels store masks only)."""
    v = pub_fields.get("tin" if key == "tin_raw" else key, "")
    if isinstance(v, dict) and v.get("masked"):
        return v["masked"]
    return mask(FIELD_KIND.get(key, "account_number"), shown) if shown else ""


def _shown_value(key: str, pub_fields: dict) -> str:
    """What the review form shows for a field. Prefers the full 'value' (present
    for banking kinds under the operator display policy) over the mask — the
    operator corrects against what they can actually read."""
    v = pub_fields.get("tin" if key == "tin_raw" else key, "")
    if isinstance(v, dict):
        if not v.get("present"):
            return ""
        return v.get("value") or v.get("masked", "")
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v or "")


def review_defaults(spec: str) -> dict:
    """Everything a form (web or terminal) needs to present the review."""
    run_id = runstore.resolve_run(spec)
    if not run_id:
        raise RunNotFound(spec)
    meta, pub, _, rep = _load_run(run_id)
    doc_class = meta.get("doc_class", "bank")
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    pub_fields = pub.get("fields", {})
    return {
        "run_id": run_id,
        "doc_class": doc_class,
        "file_name": meta.get("file_name"),
        "doc_path": meta.get("path"),
        "field_keys": [k for k in keys if k != "tin_type"],
        "booleans": sorted(BOOLEAN_KEYS & set(keys)),
        "sensitive": sorted(SENSITIVE_KEYS & set(keys)),
        "display": {k: _shown_value(k, pub_fields) for k in keys if k != "tin_type"},
        "tin_type_current": (pub_fields.get("tin") or {}).get("type", "") if doc_class == "w9" else "",
        "doc_type": pub.get("doc_type", ""),
        "doc_types": BANK_DOC_TYPES if doc_class == "bank" else W9_DOC_TYPES,
        "verdict": rep.get("verdict", ""),
        "verdicts": list(VERDICTS),
    }


def build_label(run_id: str, sub: dict) -> tuple[dict, list[str]]:
    """Apply keep/set/clear corrections to the run's extraction -> (label, secrets).
    Semantics mirror the original stdin review byte-for-byte."""
    meta, pub, stage_a_pub, rep = _load_run(run_id)
    doc_class = meta.get("doc_class", "bank")
    keys = BANK_KEYS if doc_class == "bank" else W9_KEYS
    pub_fields = pub.get("fields", {})
    corrections: dict = sub.get("fields", {}) or {}

    vault = SecretVault()
    gold_fields: dict = {}
    model_diff: dict = {}
    smap_extra: list = []

    def _keep_sensitive(k: str, entry: dict) -> None:
        gk = "tin" if k == "tin_raw" else k
        entry_copy = dict(entry)
        entry_copy.pop("value", None)   # labels NEVER store full values, any policy
        gold_fields[gk] = entry_copy
        if not entry.get("present"):
            return
        kind = FIELD_KIND.get(k, "account_number")
        n = int(entry.get("digits") or entry.get("length") or 9)
        if k == "iban":
            template = (entry.get("country") or "XX") + "0" * max(n - 2, 4)
        elif kind == "tin" and entry.get("hyphenated"):
            template = "000-00-0000" if (entry.get("type") == "SSN") else "00-0000000"
        else:
            template = "0" * n
        smap_extra.append({"kind": kind, "masked": entry.get("masked", ""),
                           "fake": fake_preserve_shape(kind, template, seed=run_id + k)})

    for k in keys:
        if k == "tin_type":
            continue  # consumed by the tin_raw set-path below
        corr = corrections.get(k) or {}
        action = corr.get("action", "keep")
        shown = _shown_value(k, pub_fields)

        if k in BOOLEAN_KEYS:
            if action == "set":
                gold_fields[k] = bool(corr.get("value"))
            else:  # keep (clear is meaningless for booleans -> keep)
                gold_fields[k] = shown == "yes"
            continue

        if k in SENSITIVE_KEYS:
            if action == "keep":
                pk = "tin" if k == "tin_raw" else k
                entry = pub_fields.get(pk)
                _keep_sensitive(k, entry if isinstance(entry, dict)
                                else {"present": False, "masked": ""})
                continue
            if action == "set" and str(corr.get("value") or "").strip():
                val = str(corr["value"]).strip()
                kind = FIELD_KIND.get(k, "account_number")
                vault.register(kind, val)
                if k == "tin_raw":
                    tt = str((corrections.get("tin_type") or {}).get("value")
                             or (pub_fields.get("tin") or {}).get("type", "") or "")
                    gold_fields["tin"] = {"type": tt.upper(), "masked": mask("tin", val),
                                          "digits": len(re.sub(r"\D", "", val)),
                                          "hyphenated": "-" in val, "present": True}
                else:
                    c = _norm_id(val)
                    entry = {"masked": mask(kind, val), "length": len(c), "present": True}
                    if k == "iban":
                        entry["country"] = c[:2] if c[:2].isalpha() else ""
                    gold_fields[k] = entry
                masked_gold = mask(kind, val)
                # 'shown' may be a FULL banking value under the display policy —
                # the diff stored in the label must be masked on both sides
                shown_masked = _masked_shown(k, pub_fields, shown)
                if masked_gold != shown_masked:
                    model_diff[k] = {"model": shown_masked, "gold": masked_gold}
            else:  # clear (or set with empty value)
                gold_fields["tin" if k == "tin_raw" else k] = {"present": False, "masked": ""}
                if shown:
                    model_diff[k] = {"model": _masked_shown(k, pub_fields, shown), "gold": ""}
            continue

        # plain text field
        if action == "set":
            val = str(corr.get("value") or "")
        elif action == "clear":
            val = ""
        else:
            val = shown
        gold_fields[k] = val
        if val != shown:
            model_diff[k] = {"model": shown, "gold": val}

    # THE LABEL PATH IS ALWAYS STRICT: under the operator display policy the run
    # artifacts legitimately hold full banking values — everything copied into a
    # training example must be re-masked here (append_label's strict gate then
    # fails closed if anything slips).
    from .privacy import scrub_text
    run_vault = SecretVault()
    for bk in _BANK_PUB_KEYS:
        e = pub_fields.get(bk)
        if isinstance(e, dict) and e.get("value"):
            run_vault.register(FIELD_KIND.get(bk, "account_number"), e["value"])
    raw_cands = stage_a_pub.get("regex_candidates_masked") or {}
    cand_masked: dict = {}
    for ck, cv in raw_cands.items():
        kind = FIELD_KIND.get(ck)
        if ck == "ssn_masked" or not cv or not kind:
            cand_masked[ck] = cv
        else:
            if kind in ("iban", "account_number", "routing_aba"):
                run_vault.register(kind, str(cv))
            cand_masked[ck] = mask(kind, str(cv))
    excerpt = scrub_text((stage_a_pub.get("raw_text_excerpt") or "")[:config.EXCERPT_LIMIT],
                         run_vault, policy="strict")

    label = {
        "label_id": f"lbl-{runstore.now_iso().replace(':', '').replace('-', '')}",
        "ts": runstore.now_iso(),
        "doc_sha256": run_id,
        "doc_path": meta.get("path"),
        "doc_class": doc_class,
        "doc_type_gold": str(sub.get("doc_type_gold") or pub.get("doc_type", "")),
        "fields_gold": gold_fields,
        "verdict_gold": str(sub.get("verdict_gold") or rep.get("verdict", "")),
        "stage_a_excerpt": excerpt,
        "regex_candidates_masked": cand_masked,
        "model_predicted": {"doc_type": pub.get("doc_type"), "fields_diff": model_diff},
        "sensitive_map": vault.sensitive_map() + smap_extra,
        "reviewer": str(sub.get("reviewer") or "egor"),
        "notes": str(sub.get("notes") or ""),
        "confirmed": True,
    }
    # union: typed corrections + full values harvested from the run artifacts —
    # the strict gate must know them all
    return label, vault.secrets() + run_vault.secrets()


def submit_review(spec: str, sub: dict) -> dict:
    run_id = runstore.resolve_run(spec)
    if not run_id:
        raise RunNotFound(spec)
    label, secrets = build_label(run_id, sub)
    append_label(label, secrets)
    return {"label_id": label["label_id"], "doc_sha256": run_id,
            "labels_count": count_labels(),
            "model_diff": label["model_predicted"]["fields_diff"]}
