"""US bank-keys validation: bankmath, predicates, rules BNK-040..046 and the
POST /api/v1/check-routing endpoint (skill sap-us-bank-validate integration).
Everything offline — the live 3-source directory ladder is not exercised here."""
import pytest
from fastapi.testclient import TestClient

from mdmdoc.fields import Extraction
from mdmdoc.rules import bankmath
from mdmdoc.rules.engine import run_rules
from mdmdoc.rules.predicates import (account_sig_digits, routing_checksum,
                                     routing_format, routing_prefix)
from mdmdoc.verdict import decide


# ---------------------------------------------------------------- bankmath ----

def test_checksum_known_good():
    # JPMorgan Chase, Bank of America, DoD/DFAS — all real, all pass 3-7-1
    for rn in ("021000021", "061112788", "041036004"):
        assert bankmath.checksum_valid(rn), rn


def test_checksum_known_bad_shows_arithmetic():
    # one-digit typo of Wells Fargo 121000248 — weighted sum 61, 61 mod 10 = 1
    assert not bankmath.checksum_valid("122000248")
    assert bankmath.checksum_sum("122000248") == 61


def test_prefix_62_is_valid_but_53_51_are_not():
    # 61-72 is the electronic-transaction range — 62 IS assigned territory
    assert bankmath.prefix_valid("620055480")
    assert not bankmath.prefix_valid("530006215")
    assert not bankmath.prefix_valid("516300391")


def test_significant_digits_ignores_sap_zero_padding():
    assert bankmath.significant_digits("000000003359888024") == 10
    assert bankmath.significant_digits("000000000000000116") == 3
    assert bankmath.significant_digits("000000000000000000") == 0


# ---------------------------------------------------------------- predicates --

def _p(fn, value, **args):
    fired, detail = fn(value, {}, args, {})
    return fired, detail


def test_routing_format_flags_bic_and_punctuation():
    fired, detail = _p(routing_format, "BOFAUS3N")
    assert fired and "SWIFT/BIC" in detail
    fired, detail = _p(routing_format, "031201467.")
    assert fired and "checksum-valid" in detail          # recoverable hint
    assert _p(routing_format, "021000021") == (False, "")
    assert _p(routing_format, "") == (False, "")          # empty = not this rule's job


def test_routing_checksum_only_judges_well_formed():
    fired, detail = _p(routing_checksum, "122000248")
    assert fired and "61" in detail and "mod 10" in detail
    assert _p(routing_checksum, "021000021") == (False, "")
    assert _p(routing_checksum, "BOFAUS3N") == (False, "")   # format rule owns it


def test_routing_prefix_needs_checksum_pass_first():
    fired, detail = _p(routing_prefix, "530006215")
    assert fired and "53" in detail
    assert _p(routing_prefix, "620055480") == (False, "")    # 62 valid
    assert _p(routing_prefix, "530006216") == (False, "")    # checksum-fail -> not ours


def test_account_sig_digits():
    fired, detail = _p(account_sig_digits, "000000000000000116", min=4)
    assert fired and "3 significant" in detail
    fired, detail = _p(account_sig_digits, "000000000000000000", min=4)
    assert fired and "all zeros" in detail
    assert _p(account_sig_digits, "000000003359888024", min=4) == (False, "")
    assert _p(account_sig_digits, "", min=4) == (False, "")


# ---------------------------------------------------------------- rules -------

def _verdict(routing=None, account=None):
    fields = {"bank_country": "US"}
    if routing:
        fields["routing_aba"] = routing
    if account:
        fields["account_number"] = account
    ext = Extraction(doc_class="bank", doc_type="", fields=fields)
    ext.register_secrets()
    findings = run_rules(ext)          # raw rules — the machine, not the gate
    return decide(findings), findings


def test_rules_end_to_end_verdicts():
    assert _verdict("021000021", "000000003359888024")[0] == "ACCEPT"
    assert _verdict("122000248")[0] == "REJECT"              # checksum
    assert _verdict("BOFAUS3N")[0] == "REJECT"               # format (BIC)
    assert _verdict("530006215")[0] == "REJECT"              # prefix
    assert _verdict("620055480")[0] == "ACCEPT"              # 62 valid + checksum ok
    assert _verdict("021000021", "000000000000000116")[0] == "NEED_MANUAL_REVIEW"


def test_wire_routing_rules_mirror():
    ext = Extraction(doc_class="bank", doc_type="",
                     fields={"bank_country": "US", "routing_aba": "021000021",
                             "routing_aba_wires": "122000248"})
    ext.register_secrets()
    findings = run_rules(ext)
    assert decide(findings) == "REJECT"
    assert any(f.rule_id == "BNK-045" for f in findings)     # wires checksum


# ---------------------------------------------------------------- endpoint ----

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("MDMDOC_MODE", "full")
    from mdmdoc.server.app import create_app
    return TestClient(create_app("full"))


def test_endpoint_rejects_empty_input(client, monkeypatch):
    r = client.post("/api/v1/check-routing", data={"routing": "", "account": ""})
    assert r.status_code == 400


def test_endpoint_gate_holds_unapproved_rules(client, monkeypatch):
    # HARD GATE on + an EMPTY approval store (every rule pending) -> nothing
    # silently ACCEPTs or REJECTs; the verdict is NMR with a RULE-GATE finding.
    # The store is monkeypatched, not read from rules/approvals.json: that file is
    # per-instance operator state, so the test must not depend on who approved what.
    from mdmdoc import rule_approvals
    monkeypatch.setattr(rule_approvals, "load", lambda: {})
    monkeypatch.setenv("MDMDOC_RULE_GATE", "1")
    r = client.post("/api/v1/check-routing", data={"routing": "122000248"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "NEED_MANUAL_REVIEW"
    assert any(f["rule_id"] == "RULE-GATE" for f in body["findings"])


def test_endpoint_raw_rules_decide_when_gate_off(client, monkeypatch):
    monkeypatch.setenv("MDMDOC_RULE_GATE", "0")
    r = client.post("/api/v1/check-routing",
                    data={"routing": "122000248", "web": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "REJECT"
    assert any(f["rule_id"] == "BNK-041" for f in body["findings"])
    assert body["web"] == [] and body["web_hint"] == ""

    r = client.post("/api/v1/check-routing",
                    data={"routing": "021000021",
                          "account": "000000003359888024", "web": "false"})
    assert r.json()["verdict"] == "ACCEPT"


def test_endpoint_masks_account_in_messages(client, monkeypatch):
    # the account value must never appear verbatim in findings (privacy vault)
    monkeypatch.setenv("MDMDOC_RULE_GATE", "0")
    r = client.post("/api/v1/check-routing",
                    data={"routing": "021000021",
                          "account": "000000000000000116", "web": "false"})
    body = r.json()
    joined = " ".join(f["message"] for f in body["findings"])
    assert "000000000000000116" not in joined


def test_endpoint_web_blocked_in_api_only(monkeypatch):
    monkeypatch.setenv("MDMDOC_MODE", "api-only")
    from mdmdoc.server.app import create_app
    c = TestClient(create_app("api-only"))
    r = c.post("/api/v1/check-routing",
               data={"routing": "021000021", "web": "true"})
    assert r.status_code == 400
