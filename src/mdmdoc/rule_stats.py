#!/usr/bin/env python3
"""
rule_stats.py — per-rule firing statistics + tier-promotion PROPOSALS (П7, R2).

Governance model: rules carry hash-immune tier metadata (corp | experimental |
learned; F5 denylist). This module joins historical run artifacts with the
operator's CONFIRMED labels to compute, per rule, how often its severity was
JUSTIFIED (the confirmed gold verdict was at least as strict as the rule's
verdict_effect), and derives promotion/demotion PROPOSALS from Wilson lower
bounds. Proposals are NEVER auto-applied — the operator approves each one in
the panel, which performs a surgical tier-line edit (rules_io.set_rule_tier).
Because `tier` is excluded from the approval hash (rule_approvals._METADATA_KEYS),
promoting a rule does NOT reset its approval — the hard gate is untouched.

Standalone by design: `collect(runs_dir, labels_path)` takes explicit paths so
the aggregator can run over the mini's history from any host. Writes only
eval/rule_stats.json (rule ids + counts, no PII).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config
from .verdict import RANK

# Promotion policy (Wilson-LB thresholds are reconciled with tiny-n reality:
# a perfect 10/10 gives LB 0.722 — an LB-0.9 bar would be unreachable below
# n≈35, so the bar is 'perfect-or-near-perfect at the required n').
POLICY = {
    ("experimental", "corp"): {"min_n": 10, "min_lb": 0.60, "min_age_days": 14},
    ("learned", "experimental"): {"min_n": 5, "min_lb": 0.45, "min_age_days": 7},
}
DEMOTION = {
    "corp": {"min_n": 10, "max_lb": 0.40, "to": "experimental"},
    "experimental": {"min_n": 8, "max_lb": 0.30, "to": "learned"},
}
_NEXT_TIER = {"experimental": "corp", "learned": "experimental"}


def collect(runs_dir: Path | None = None, labels_path: Path | None = None) -> list[dict]:
    """One row per (run, fired rule id): joins runs/*/findings.json with the
    confirmed labels (by doc_sha256 == run dir name). Pure reads."""
    runs = Path(runs_dir or config.RUNS_DIR)
    lpath = Path(labels_path or config.LABELS_PATH)
    labels: dict[str, dict] = {}
    if lpath.exists():
        for line in lpath.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lab = json.loads(line)
                labels[str(lab.get("doc_sha256") or "")] = lab
    rows: list[dict] = []
    if not runs.exists():
        return rows
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            findings = json.loads((d / "findings.json").read_text(encoding="utf-8"))
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        lab = labels.get(d.name)
        for f in findings:
            if not isinstance(f, dict) or not f.get("rule_id"):
                continue
            rows.append({"run_id": d.name, "ts": str(meta.get("ts") or ""),
                         "doc_class": str(meta.get("doc_class") or ""),
                         "rule_id": str(f["rule_id"]),
                         "confirmed": bool(lab and lab.get("confirmed")),
                         "verdict_gold": str((lab or {}).get("verdict_gold") or "")})
    return rows


def _wilson_lb(hits: int, n: int) -> float:
    from .evalrun import _wilson
    return _wilson(hits, n)[0]


def _age_days(first_ts: str, now_iso: str) -> float | None:
    from datetime import datetime
    try:
        a = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        b = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 86400.0
    except ValueError:
        return None


def compute(rows: list[dict], rules_by_class: dict[str, list[dict]],
            approvals: dict | None = None, now_iso: str = "",
            challenge_counts: dict[str, int] | None = None) -> list[dict]:
    """Per-rule stats. precision = of the fired-on-CONFIRMED docs, the fraction
    whose confirmed gold verdict is at least as strict as the rule's
    verdict_effect (its severity was justified). NOTE/null-effect rules get
    precision None and are never proposed. `challenge_counts` (E5) joins the
    live operator-contradiction ledger — demotion evidence."""
    if not now_iso:
        from . import runstore
        now_iso = runstore.now_iso()
    approvals = approvals or {}
    if challenge_counts is None:
        from . import challenges
        challenge_counts = challenges.counts()
    out: list[dict] = []
    for doc_class, rules in rules_by_class.items():
        for rule in rules:
            rid = str(rule.get("id") or "")
            eff = rule.get("verdict_effect")
            mine = [r for r in rows if r["rule_id"] == rid
                    and (not r["doc_class"] or r["doc_class"] == doc_class)]
            fired = len(mine)
            conf = [r for r in mine if r["confirmed"]]
            hits = n = 0
            precision = lb = None
            if eff in RANK:
                n = len(conf)
                hits = sum(1 for r in conf
                           if RANK.get(r["verdict_gold"], -1) >= RANK[eff])
                if n:
                    precision = round(hits / n, 3)
                    lb = _wilson_lb(hits, n)
            entry = approvals.get(f"{doc_class}:{rid}") or {}
            first_ts = str(entry.get("ts") or "") or min(
                (r["ts"] for r in mine if r["ts"]), default="")
            age = _age_days(first_ts, now_iso) if first_ts else None
            out.append({"rule_id": rid, "doc_class": doc_class,
                        "tier": str(rule.get("tier") or ""),
                        "verdict_effect": eff,
                        "fired": fired, "fired_confirmed": n, "hits": hits,
                        "precision": precision, "wilson_lb": lb,
                        "age_days": round(age, 1) if age is not None else None,
                        "first_seen": first_ts,
                        "challenges": int(challenge_counts.get(rid, 0))})
    return out


CHALLENGE_DEMOTION_AT = 2   # ≥2 live operator contradictions -> propose demotion


def propose(stats: list[dict]) -> list[dict]:
    """Promotion/demotion PROPOSALS from the policy tables. Informational —
    application is a human panel action."""
    proposals: list[dict] = []
    for s in stats:
        tier = s["tier"]
        ch = int(s.get("challenges") or 0)
        if ch >= CHALLENGE_DEMOTION_AT and tier in ("corp", "experimental", "learned"):
            # operator judgement contradicted this rule repeatedly (E5): corp
            # steps down a tier; experimental/learned -> reject-or-delete review
            to = "experimental" if tier == "corp" else "reject-or-delete"
            proposals.append({"rule_id": s["rule_id"], "doc_class": s["doc_class"],
                              "kind": "demotion", "from_tier": tier, "to_tier": to,
                              "evidence": {"challenges": ch}})
        lb, n, age = s["wilson_lb"], s["fired_confirmed"], s["age_days"]
        if s["precision"] is None or lb is None:
            continue    # NOTE/null-effect or never fired on confirmed docs
        nxt = _NEXT_TIER.get(tier)
        pol = POLICY.get((tier, nxt)) if nxt else None
        if pol and n >= pol["min_n"] and lb >= pol["min_lb"] \
                and age is not None and age >= pol["min_age_days"]:
            proposals.append({"rule_id": s["rule_id"], "doc_class": s["doc_class"],
                              "kind": "promotion", "from_tier": tier, "to_tier": nxt,
                              "evidence": {"n": n, "hits": s["hits"],
                                           "wilson_lb": lb, "age_days": age}})
        dem = DEMOTION.get(tier)
        if dem and n >= dem["min_n"] and lb <= dem["max_lb"]:
            proposals.append({"rule_id": s["rule_id"], "doc_class": s["doc_class"],
                              "kind": "demotion", "from_tier": tier,
                              "to_tier": dem["to"],
                              "evidence": {"n": n, "hits": s["hits"], "wilson_lb": lb}})
    return proposals


def build(runs_dir: Path | None = None, labels_path: Path | None = None) -> dict:
    """collect -> compute -> propose over the local (or given) state."""
    import yaml

    from . import rule_approvals, rules_io, runstore
    rows = collect(runs_dir, labels_path)
    rules_by_class = {}
    for dc in ("bank", "w9"):
        text = rules_io.rules_text(dc)
        cfg = yaml.safe_load(text) if text else {}
        rules_by_class[dc] = [r for r in (cfg or {}).get("rules", []) if isinstance(r, dict)]
    stats = compute(rows, rules_by_class, approvals=rule_approvals.load())
    return {"ts": runstore.now_iso(), "per_rule": stats, "proposals": propose(stats)}


def write_report(payload: dict) -> Path:
    config.ensure_dirs()
    p = config.EVAL_DIR / "rule_stats.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
