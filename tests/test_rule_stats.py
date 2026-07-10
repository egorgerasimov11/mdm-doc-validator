"""П7/R2: per-rule precision stats + tier promotion PROPOSALS. Fixture-driven
(no network, no model): synthetic runs dir + labels file + approvals."""
import json

import pytest

from mdmdoc import config, rule_stats, rules_io
from mdmdoc.rule_stats import collect, compute, propose

NOW = "2026-07-09T12:00:00Z"
OLD = "2026-06-01T00:00:00Z"


def _mk_run(runs, rid, rule_ids, ts=OLD, doc_class="bank"):
    d = runs / rid
    d.mkdir(parents=True)
    (d / "findings.json").write_text(json.dumps(
        [{"rule_id": r, "severity": "WARNING", "verdict_effect": "NEED_MANUAL_REVIEW",
          "message": "m"} for r in rule_ids]))
    (d / "meta.json").write_text(json.dumps(
        {"run_id": rid, "ts": ts, "doc_class": doc_class}))
    (d / "report.json").write_text(json.dumps({"verdict": "NEED_MANUAL_REVIEW"}))


def _mk_labels(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


RULE = {"id": "X-024", "name": "t", "tier": "experimental", "source": "skill",
        "when": {"always": True}, "severity": "WARNING",
        "verdict_effect": "NEED_MANUAL_REVIEW", "message": "m"}
NOTE_RULE = {"id": "X-005", "name": "n", "tier": "corp", "source": "skill",
             "when": {"always": True}, "severity": "NOTE",
             "verdict_effect": None, "message": "m"}


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    labels = tmp_path / "labels.jsonl"
    entries = []
    for i in range(12):
        rid = f"run{i:02d}" + "x" * 10
        _mk_run(runs, rid, ["X-024", "X-005"])
        if i < 10:   # 10 confirmed, gold at-least-as-strict (NMR/REJECT)
            entries.append({"doc_sha256": rid, "confirmed": True,
                            "verdict_gold": "REJECT" if i % 2 else "NEED_MANUAL_REVIEW"})
    _mk_labels(labels, entries)
    approvals = {"bank:X-024": {"status": "approved", "hash": "h", "ts": OLD},
                 "bank:X-005": {"status": "approved", "hash": "h", "ts": OLD}}
    return runs, labels, approvals


def test_join_precision_and_promotion(env):
    runs, labels, approvals = env
    rows = collect(runs, labels)
    stats = compute(rows, {"bank": [RULE, NOTE_RULE]}, approvals, now_iso=NOW)
    s = next(x for x in stats if x["rule_id"] == "X-024")
    assert s["fired"] == 12 and s["fired_confirmed"] == 10
    assert s["precision"] == 1.0
    assert 0.70 <= s["wilson_lb"] <= 0.75          # 10/10 -> LB ~0.722
    props = propose(stats)
    assert any(p["rule_id"] == "X-024" and p["kind"] == "promotion"
               and p["to_tier"] == "corp" for p in props)


def test_note_rules_never_proposed(env):
    runs, labels, approvals = env
    stats = compute(collect(runs, labels), {"bank": [NOTE_RULE]}, approvals, now_iso=NOW)
    s = stats[0]
    assert s["precision"] is None and s["wilson_lb"] is None
    assert propose(stats) == []


def test_policy_boundaries(env):
    runs, labels, approvals = env
    rows = collect(runs, labels)
    # n=9 -> no proposal
    stats = compute([r for r in rows if r["run_id"] < "run09"][:99],
                    {"bank": [RULE]}, approvals, now_iso=NOW)
    assert all(x["fired_confirmed"] <= 9 for x in stats)
    assert propose(stats) == []
    # 8/10 hits -> LB ~0.49 < 0.60 -> no proposal
    rows2 = collect(runs, labels)
    flip = 0
    for r in rows2:
        if r["confirmed"] and r["rule_id"] == "X-024" and flip < 2:
            r["verdict_gold"] = "ACCEPT"    # gold SOFTER than the rule's effect
            flip += 1
    stats = compute(rows2, {"bank": [RULE]}, approvals, now_iso=NOW)
    s = stats[0]
    assert s["hits"] == 8 and s["wilson_lb"] < 0.60
    assert propose(stats) == []
    # young rule -> no proposal
    fresh = {"bank:X-024": {"status": "approved", "hash": "h",
                            "ts": "2026-07-05T00:00:00Z"}}
    stats = compute(collect(runs, labels), {"bank": [RULE]}, fresh, now_iso=NOW)
    assert propose(stats) == []


def test_demotion_signal(env):
    runs, labels, approvals = env
    rows = collect(runs, labels)
    for r in rows:
        if r["confirmed"]:
            r["verdict_gold"] = "ACCEPT"    # every confirmed gold softer -> 0 hits
    corp_rule = dict(RULE, tier="corp")
    stats = compute(rows, {"bank": [corp_rule]}, approvals, now_iso=NOW)
    props = propose(stats)
    assert any(p["kind"] == "demotion" and p["to_tier"] == "experimental"
               for p in props)


def test_unconfirmed_labels_excluded(tmp_path):
    runs = tmp_path / "runs"
    labels = tmp_path / "labels.jsonl"
    rid = "runX" + "y" * 12
    _mk_run(runs, rid, ["X-024"])
    _mk_labels(labels, [{"doc_sha256": rid, "confirmed": False,
                         "verdict_gold": "REJECT"}])
    stats = compute(collect(runs, labels), {"bank": [RULE]}, {}, now_iso=NOW)
    assert stats[0]["fired"] == 1 and stats[0]["fired_confirmed"] == 0


def test_empty_runs_dir_is_fine(tmp_path):
    stats = compute(collect(tmp_path / "nope", tmp_path / "nope.jsonl"),
                    {"bank": [RULE]}, {}, now_iso=NOW)
    assert stats[0]["fired"] == 0 and propose(stats) == []


def test_set_rule_tier_surgical(tmp_path, monkeypatch):
    """Tier edit preserves in-block comments byte-for-byte and never touches
    the approval hash (the F5 denylist property, verified end-to-end)."""
    import shutil

    from mdmdoc import rule_approvals
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    import mdmdoc.rules_io as real_io
    real_uni = __import__("pathlib").Path(__file__).resolve().parents[1] / "rules" / "rules.yaml"
    text_real = real_io._split_sections(real_uni.read_text())["bank"]
    (tmp_path / "banking.yaml").write_text(text_real)
    text_before = (tmp_path / "banking.yaml").read_text()
    import yaml
    rule_before = next(r for r in yaml.safe_load(text_before)["rules"]
                       if r["id"] == "BNK-030")
    h_before = rule_approvals.rule_hash(rule_before)
    res = rules_io.set_rule_tier("bank", "BNK-030", "corp")
    assert res["old_tier"] == "experimental" and res["new_tier"] == "corp"
    text_after = (tmp_path / "banking.yaml").read_text()
    # only the one tier line changed — the in-block comments survive
    diff = [(a, b) for a, b in zip(text_before.splitlines(),
                                   text_after.splitlines()) if a != b]
    assert diff == [("    tier: experimental", "    tier: corp")]
    rule_after = next(r for r in yaml.safe_load(text_after)["rules"]
                      if r["id"] == "BNK-030")
    assert rule_approvals.rule_hash(rule_after) == h_before   # approval survives
    assert rules_io.set_rule_tier("bank", "BNK-030", "learned")["old_tier"] == "corp"
    with pytest.raises(ValueError):
        rules_io.set_rule_tier("bank", "BNK-030", "platinum")
    with pytest.raises(ValueError):
        rules_io.set_rule_tier("bank", "NOPE-1", "corp")
