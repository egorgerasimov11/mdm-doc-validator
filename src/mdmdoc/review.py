#!/usr/bin/env python3
"""
review.py — terminal front-end for the teach loop. All decision logic lives in
review_core; this module only maps stdin answers (Enter = keep, '-' = clear,
typed = set) onto a ReviewSubmission and prints the result.
"""
from __future__ import annotations

import subprocess

from . import review_core


def _ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt).strip()
    except EOFError:
        return default
    return s if s else default


def review_run(spec: str, open_doc: bool = False) -> int:
    try:
        form = review_core.review_defaults(spec)
    except review_core.RunNotFound:
        print(f"run not found: {spec} (try `mdmdoc runs`)")
        return 1
    run_id = form["run_id"]
    print(f"\nReviewing run {run_id} — {form['file_name']} ({form['doc_class']})")
    if open_doc and form.get("doc_path"):
        subprocess.run(["open", form["doc_path"]], check=False)

    print("For each field: Enter = keep the model's value, or type the correction "
          "('-' clears the field).")
    fields: dict = {}
    for k in form["field_keys"]:
        shown = form["display"].get(k, "")
        if k in form["booleans"]:
            ans = _ask(f"  {k:24} [{shown or 'no'}] (y/n): ")
            if ans:
                fields[k] = {"action": "set", "value": ans.lower().startswith("y")}
            continue
        ans = _ask(f"  {k:24} [{shown}]: ")
        if not ans:
            continue  # keep is the default in review_core
        if ans == "-":
            fields[k] = {"action": "clear", "value": None}
            continue
        fields[k] = {"action": "set", "value": ans}
        if k == "tin_raw":
            tt = _ask(f"  {'tin_type':24} [{form.get('tin_type_current', '')}]: ",
                      form.get("tin_type_current", ""))
            fields["tin_type"] = {"action": "set", "value": tt}

    print(f"\nDoc types: {', '.join(form['doc_types'])}")
    doc_type_gold = _ask(f"doc_type [{form['doc_type']}]: ", form["doc_type"])
    print(f"Verdicts: {', '.join(form['verdicts'])}")
    verdict_gold = _ask(f"verdict [{form['verdict']}]: ", form["verdict"])
    notes = _ask("notes (optional): ")
    print(f"Scenario tags: {', '.join(form['scenario_options'])}")
    suggested = ", ".join(form["scenarios_suggested"])
    scen = _ask(f"scenarios (comma-separated) [{suggested}]: ", suggested)
    print(f"Error sources: {', '.join(form['error_sources'])} (blank = model was right)")
    error_source = _ask("error_source []: ")

    result = review_core.submit_review(run_id, {
        "fields": fields, "doc_type_gold": doc_type_gold,
        "verdict_gold": verdict_gold, "notes": notes,
        "scenarios": [s.strip() for s in scen.split(",") if s.strip()],
        "error_source": error_source,
    })
    print(f"\nSaved label for {form['file_name']} — dataset now has "
          f"{result['labels_count']} examples.")
    from .fewshot import build_fewshot
    build_fewshot(k=2)   # feedback trains immediately — no separate step to forget
    print("Few-shot exemplars rebuilt. Your verdict/doc_type now override the "
          "machine for this document (operator precedent). "
          "To retrain the extractor: build & evaluate a candidate on the Training "
          "page (adoption is gated — leakage 0, invoice false-accepts 0, no "
          "critical-field regressions), then Adopt.")
    return 0
