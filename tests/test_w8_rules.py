"""W2: the W-8 rule pack (experimental, PENDING by design) + the ch4-cert
predicate. Verdict invariance: while PENDING, the approved W9-030 alone keeps
every W-8 at NEED_MANUAL_REVIEW — shipping these rules changes nothing."""
from mdmdoc.fields import W8_KEYS, Extraction
from mdmdoc.rules.engine import run_rules
from mdmdoc.rules.predicates import w8_ch4_cert_missing
from mdmdoc.verdict import decide


def _ext(**over):
    e = Extraction(doc_class="w9", doc_type="w8")
    e.fields = {k: "" for k in W8_KEYS}
    e.fields["signed"] = False
    e.fields["capacity_checked"] = False
    e.fields.update(over)
    return e


def _ids(findings):
    return {f.rule_id for f in findings}


def test_predicate_fires_only_on_unbacked_claim():
    fired, detail = w8_ch4_cert_missing(
        "", {"chapter4_status": "Active NFFE", "chapter4_cert_section": ""}, {}, {})
    assert fired and "Active NFFE" in detail
    assert not w8_ch4_cert_missing(
        "", {"chapter4_status": "Active NFFE", "chapter4_cert_section": "Part XXV"},
        {}, {})[0]
    assert not w8_ch4_cert_missing(
        "", {"chapter4_status": "", "chapter4_cert_section": ""}, {}, {})[0]


def test_w8_rules_fire_unenforced():
    ext = _ext(chapter4_status="Active NFFE")
    ids = _ids(run_rules(ext, enforce_approvals=False))
    assert {"W9-030", "W8-001", "W8-002", "W8-003", "W8-005"} <= ids
    assert "W8-004" not in ids                    # no sign_date -> stale silent


def test_complete_signed_w8_only_notes():
    ext = _ext(legal_name="Nord Fake GmbH", country_incorporation="Germany",
               chapter4_status="Active NFFE", chapter4_cert_section="Part XXV",
               signed=True, sign_date="02-03-2026", signer_name="Karl Mustermann")
    ids = _ids(run_rules(ext, enforce_approvals=False))
    assert not ids & {"W8-001", "W8-002", "W8-003", "W8-005"}
    assert "W9-030" in ids                        # approved catch-all remains


def test_pending_rules_change_no_verdict(tmp_path, monkeypatch):
    """The live gate, modeled on the mini's state (W9-030 approved, W8-* not):
    pending W-8 rules surface only as RULE-GATE and the verdict stays exactly
    what it is today — NEED_MANUAL_REVIEW via W9-030."""
    import shutil

    from mdmdoc import config, rule_approvals
    from mdmdoc.rules import engine
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    from mdmdoc import rules_io
    (rules_dir / "w9.yaml").write_text(rules_io.rules_text("w9"))
    monkeypatch.setattr(config, "RULES_DIR", rules_dir)
    w9_030 = next(r for r in engine.load_rules("w9")["rules"] if r["id"] == "W9-030")
    rule_approvals.set_decision("w9", w9_030, "approved")

    ext = _ext(chapter4_status="Active NFFE")
    findings = run_rules(ext, enforce_approvals=True)
    ids = _ids(findings)
    assert "W9-030" in ids and "RULE-GATE" in ids
    assert not ids & {"W8-001", "W8-002", "W8-003", "W8-004", "W8-005"}
    assert decide(findings) == "NEED_MANUAL_REVIEW"
