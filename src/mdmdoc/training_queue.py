#!/usr/bin/env python3
"""
training_queue.py — which documents are worth labeling NEXT.

The teach loop only learns from what the operator labels; this module ranks
runs by expected teaching value so high-signal documents don't sit unnoticed:

  - unlabeled runs the machine itself was unsure about (NEED_MANUAL_REVIEW /
    WARNING verdicts, strong-tier escalations, model-vs-evidence conflicts);
  - unlabeled runs showing a scenario no label covers yet;
  - LABELED runs that regressed in the last eval — stale gold is a real
    failure mode (perception improves, old labels freeze old model quality).
"""
from __future__ import annotations

import json

from . import config, runstore, scenarios
from .dataset import load_labels

# extraction warnings that mean "the model and deterministic evidence disagreed"
_CONFLICT_MARKERS = ("overrode", "disagreement", "echo", "visual checkbox",
                     "vision says", "TIN-box crop")


def build_queue(limit: int = 12) -> list[dict]:
    labels = load_labels()
    labeled_ids = {l.get("doc_sha256") for l in labels}
    covered_tags = {t for l in labels for t in (l.get("scenarios") or [])}

    # eval regressions by run_id (rows carry run_id + file; diff carries files)
    regressed_files: set = set()
    still_wrong_files: set = set()
    p = config.EVAL_DIR / "last_results.json"
    if p.exists():
        try:
            res = json.loads(p.read_text())
            regressed_files = set(res.get("diff", {}).get("regressed") or [])
            still_wrong_files = set(res.get("diff", {}).get("unchanged_wrong") or [])
        except Exception:
            pass

    from . import ratings as _ratings
    thumb = _ratings.latest()

    out: list[dict] = []
    for row in runstore.list_runs():
        rid = row["run_id"]
        meta = runstore.load(rid, "meta.json") or {}
        pub = runstore.load(rid, "extraction.json") or {}
        stage_a_pub = runstore.load(rid, "stage_a.json") or {}
        findings = runstore.load(rid, "findings.json") or []
        labeled = rid in labeled_ids
        score = 0
        reasons: list[str] = []
        if thumb.get(rid) == "down":
            score += 4
            reasons.append("operator flagged the analysis 👎 — label this first")

        if labeled:
            # labeled runs only surface when the last eval says the gold went stale
            if row["file"] in regressed_files:
                score += 3
                reasons.append("regressed in the last eval — label may be stale")
            elif row["file"] in still_wrong_files:
                score += 1
                reasons.append("still wrong after training — re-check label or rules")
        else:
            if row.get("verdict") == "NEED_MANUAL_REVIEW":
                score += 3
                reasons.append("machine asked for manual review")
            elif row.get("verdict") == "WARNING":
                score += 2
                reasons.append("WARNING verdict")
            if meta.get("escalated_because"):
                score += 2
                reasons.append("escalated to strong tier: "
                               + ", ".join(map(str, meta["escalated_because"])))
            conflicts = [w for w in (pub.get("warnings") or [])
                         if any(m in str(w) for m in _CONFLICT_MARKERS)]
            if conflicts:
                score += 2
                reasons.append(f"model/evidence conflict ({len(conflicts)} warning(s))")
            fresh = [t for t in scenarios.suggest(meta, stage_a_pub, pub, findings)
                     if t not in covered_tags]
            if fresh:
                score += 2
                reasons.append("uncovered scenario: " + ", ".join(fresh))

        if score:
            out.append({"run_id": rid, "file": row["file"],
                        "doc_class": row.get("doc_class", "?"),
                        "verdict": row.get("verdict", "?"), "ts": row.get("ts", ""),
                        "labeled": labeled, "score": score, "reasons": reasons})

    out.sort(key=lambda r: (r["score"], r["ts"]), reverse=True)  # score, then newest
    return out[:limit]
