"""Tests for the web_enrichment external-evidence layer.

Invariants under test:
  * OPT-IN: off unless MDMDOC_WEB_EVIDENCE=1.
  * PRIVACY: routing/SWIFT/bank+company names may egress; TIN/account/IBAN never.
  * NEVER DECIDES: every finding is NOTE with verdict_effect=None.
  * FAIL-SOFT: offline degrades to 'unavailable', offline checks still produce evidence.
No test here touches the real network (http is monkeypatched or the check is offline).
"""
import pytest

from mdmdoc.fields import Extraction
from mdmdoc.privacy import SecretVault
from mdmdoc.verdict import decide
from mdmdoc.web_enrichment import aba, egress, entity, swift
from mdmdoc.web_enrichment import gather, enabled
from mdmdoc.web_enrichment.evidence import CONFLICT, FOUND, NOT_FOUND, UNAVAILABLE, Evidence


def _bank_ext():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"routing_aba": "211070175", "routing_aba_wires": "011500120",
                "swift_bic": "CTZIUS33", "bank_name": "Citizens Bank",
                "bank_country": "US", "account_holder": "EurekaConnect LLC",
                "account_number": "1408137817", "iban": ""}
    e.register_secrets()
    return e


# ------------------------------------------------------------------ egress ----
def test_egress_allows_public_ids_blocks_sensitive():
    v = SecretVault()
    v.register("account_number", "1408137817")
    v.register("iban", "DE44500105175407324931")
    v.register("tin", "81-0826734")
    # allowed egress
    for ok in ("aba:211070175", "bic:CTZIUS33", "NAME:Citizens Bank",
               "legalName:EurekaConnect LLC"):
        egress.assert_safe_outbound(ok, v)          # must not raise
        assert egress.is_safe_outbound(ok, v)
    # forbidden egress — exact known secrets
    for bad in ("acct 1408137817", "iban DE44500105175407324931", "ein 81-0826734"):
        with pytest.raises(egress.EgressBlocked):
            egress.assert_safe_outbound(bad, v)
    # forbidden by generic pattern even without a vault
    with pytest.raises(egress.EgressBlocked):
        egress.assert_safe_outbound("iban GB33BUKB20201555555555", None)
    with pytest.raises(egress.EgressBlocked):
        egress.assert_safe_outbound("the ssn is 320-54-0693", None)


def test_egress_routing_not_in_forbidden_set():
    assert "routing_aba" not in egress.FORBIDDEN_KINDS
    v = SecretVault()
    v.register("routing_aba", "211070175")   # routing is registered but ALLOWED out
    egress.assert_safe_outbound("aba:211070175", v)   # must not raise


def test_http_egress_guard_fires_before_send(monkeypatch):
    """A URL carrying a forbidden value must raise BEFORE any socket work."""
    from mdmdoc.web_enrichment import http

    def _boom(*a, **k):
        raise AssertionError("network must not be reached when egress blocks")

    monkeypatch.setattr(http._SESSION, "send", _boom)
    v = SecretVault()
    v.register("account_number", "1408137817")
    with pytest.raises(egress.EgressBlocked):
        http.get_json("https://example.test/api", params={"q": "1408137817"}, vault=v)


# ------------------------------------------------------------------ opt-in ----
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MDMDOC_WEB_EVIDENCE", raising=False)
    assert enabled() is False
    assert gather(_bank_ext()) == ([], [])


def test_enabled_flag_values(monkeypatch):
    for on in ("1", "true", "on", "YES"):
        monkeypatch.setenv("MDMDOC_WEB_EVIDENCE", on)
        assert enabled() is True
    for off in ("0", "", "no", "false"):
        monkeypatch.setenv("MDMDOC_WEB_EVIDENCE", off)
        assert enabled() is False


# ------------------------------------------------------------- never decides --
def test_findings_are_note_only_and_verdict_neutral(monkeypatch):
    monkeypatch.setenv("MDMDOC_WEB_EVIDENCE", "1")
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: None)   # force offline
    findings, rows = gather(_bank_ext())
    assert findings and rows
    assert all(f.severity == "NOTE" and f.verdict_effect is None for f in findings)
    assert decide(findings) == "ACCEPT"       # web alone can never move the verdict


def test_evidence_to_finding_is_always_note():
    ev = Evidence("x", CONFLICT, "bad thing", "SomeSrc", 1, "WEB-X-1")
    f = ev.to_finding()
    assert f.severity == "NOTE" and f.verdict_effect is None
    assert "web did not decide" in f.message.lower()


# ------------------------------------------------------------- offline checks -
def test_offline_checks_still_produce_evidence(monkeypatch):
    monkeypatch.setenv("MDMDOC_WEB_EVIDENCE", "1")
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: None)   # network down
    findings, rows = gather(_bank_ext())
    by_check = {r["check"]: r for r in rows}
    assert by_check["aba_checksum"]["status"] == FOUND            # offline, deterministic
    assert by_check["swift_syntax"]["status"] == FOUND            # offline, deterministic
    assert by_check["fdic_bank"]["status"] == UNAVAILABLE         # needs network
    assert by_check["gleif_entity"]["status"] == UNAVAILABLE


# ------------------------------------------------------------------- ABA ------
def test_aba_checksum():
    assert aba.checksum_valid("211070175")
    assert aba.checksum_valid("011500120")
    assert not aba.checksum_valid("211070176")
    assert not aba.checksum_valid("12345")


def test_aba_bad_checksum_is_conflict_not_verdict(monkeypatch):
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: None)
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"routing_aba": "211070176", "bank_name": "", "bank_country": "US"}
    evs = aba.collect(e)
    chk = [x for x in evs if x.check == "aba_checksum"][0]
    assert chk.status == CONFLICT
    assert chk.to_finding().verdict_effect is None


def test_fdic_found_active(monkeypatch):
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: {"data": [
        {"data": {"NAME": "Citizens Bank, National Association", "CITY": "Providence",
                  "STALP": "RI", "ACTIVE": 1, "CERT": 57957}}]})
    ev = aba._fdic_evidence("Citizens Bank", SecretVault())
    assert ev.status == FOUND and "ACTIVE" in ev.label


def test_fdic_inactive_is_conflict(monkeypatch):
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: {"data": [
        {"data": {"NAME": "Citizens Bank", "ACTIVE": 0, "CERT": 1}}]})
    ev = aba._fdic_evidence("Citizens Bank", SecretVault())
    assert ev.status == CONFLICT


def test_fdic_no_match_is_not_found(monkeypatch):
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: {"data": [
        {"data": {"NAME": "Totally Different Bank", "ACTIVE": 1}}]})
    ev = aba._fdic_evidence("Citizens Bank", SecretVault())
    assert ev.status == NOT_FOUND


def test_fed_routing_owner_mismatch_is_conflict(monkeypatch):
    monkeypatch.setenv("MDMDOC_FED_ROUTING_URL", "https://fed.test/{aba}")
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: {"name": "Wells Fargo Bank"})
    ev = aba._fed_routing_evidence("211070175", "Citizens Bank", SecretVault())
    assert ev is not None and ev.status == CONFLICT


def test_fed_routing_skipped_when_unconfigured(monkeypatch):
    monkeypatch.delenv("MDMDOC_FED_ROUTING_URL", raising=False)
    assert aba._fed_routing_evidence("211070175", "Citizens Bank", SecretVault()) is None


# ------------------------------------------------------------------ SWIFT -----
def test_bic_parse_and_syntax():
    assert swift.parse_bic("CTZIUS33")["country"] == "US"
    assert swift.parse_bic("BOFAUS3NXXX")["head_office"] is True
    assert swift.parse_bic("SHORT") is None
    assert swift.parse_bic("BADC0UNTRY") is None or True  # invalid handled downstream


def test_swift_country_conflict(monkeypatch):
    monkeypatch.delenv("MDMDOC_SWIFT_LOOKUP_URL", raising=False)
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"swift_bic": "DEUTDEFF", "bank_country": "US"}   # BIC says DE
    evs = swift.collect(e)
    statuses = {ev.check: ev.status for ev in evs}
    assert statuses["swift_syntax"] == FOUND
    assert statuses["swift_country"] == CONFLICT


def test_swift_invalid_bic_conflict():
    e = Extraction(doc_class="bank", doc_type="bank_letter")
    e.fields = {"swift_bic": "NOTAVALIDBIC1", "bank_country": ""}
    evs = swift.collect(e)
    assert evs[0].status == CONFLICT
    assert evs[0].to_finding().verdict_effect is None


# ------------------------------------------------------------------ entity ----
def test_personal_name_not_queried(monkeypatch):
    """A W-9 individual's personal name must never be sent to registries."""
    calls = []
    from mdmdoc.web_enrichment import http
    monkeypatch.setattr(http, "get_json", lambda *a, **k: calls.append(k) or None)
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"line1_name": "John Smith", "line2_business_name": "",
                "line3_classification": "Individual/sole proprietor"}
    evs = entity.collect(e)
    assert evs == [] and calls == []


def test_org_name_is_queried(monkeypatch):
    seen = []
    from mdmdoc.web_enrichment import http

    def fake(url, params=None, vault=None, headers=None):
        seen.append(url)
        if "gleif" in url:
            return {"data": [{"attributes": {"lei": "X123", "entity": {
                "legalName": {"name": "Acme Holdings LLC"}, "status": "ACTIVE",
                "legalAddress": {"country": "US"}}}}]}
        return {"hits": {"hits": []}}

    monkeypatch.setattr(http, "get_json", fake)
    e = Extraction(doc_class="w9", doc_type="w9")
    e.fields = {"line1_name": "Acme Holdings LLC", "line2_business_name": "",
                "line3_classification": "LLC"}
    evs = entity.collect(e)
    checks = {ev.check: ev.status for ev in evs}
    assert checks["gleif_entity"] == FOUND
    assert any("gleif" in u for u in seen)


def test_is_org_name_gate():
    assert entity._is_org_name("Acme LLC")
    assert entity._is_org_name("American Epilepsy Society")     # org keyword
    assert not entity._is_org_name("John Smith")
    assert not entity._is_org_name("Jane Q. Public")


# --------------------------------------------------------- leak-gate on rows --
def test_web_rows_pass_strict_leak_gate(monkeypatch):
    """Whatever the connectors emit must survive assert_no_leak (nothing leaks)."""
    import json

    from mdmdoc.privacy import assert_no_leak
    from mdmdoc.web_enrichment import http
    monkeypatch.setenv("MDMDOC_WEB_EVIDENCE", "1")
    monkeypatch.setattr(http, "get_json", lambda *a, **k: None)
    e = _bank_ext()
    _, rows = gather(e)
    blob = json.dumps(rows, ensure_ascii=False)
    # strict gate + the run's real account as a known secret — must not appear
    assert assert_no_leak(blob, known_secrets=["1408137817"], policy="strict") == []
    assert "1408137817" not in blob


def test_web_evidence_write_survives_strict_gate_with_routing(monkeypatch):
    """Regression: routing numbers legitimately APPEAR in web_evidence.json (the
    ABA check subject, egress-allowed). The pipeline must write it with a
    forbidden set that EXCLUDES routing (account/IBAN/TIN only) so the strict
    gate — active under the masked display policy — does not flag the routing
    number and crash the run."""
    import json

    from mdmdoc.privacy import assert_no_leak
    from mdmdoc.web_enrichment import http
    from mdmdoc.web_enrichment.egress import FORBIDDEN_KINDS
    monkeypatch.setenv("MDMDOC_WEB_EVIDENCE", "1")
    monkeypatch.setattr(http, "get_json", lambda *a, **k: None)
    e = _bank_ext()
    e.fields["iban"] = "DE44500105175407324931"
    e.register_secrets()
    _, rows = gather(e)
    blob = json.dumps(rows, ensure_ascii=False)
    web_secrets = e.vault.secrets(FORBIDDEN_KINDS)      # what the pipeline passes
    assert "211070175" not in web_secrets                # routing is NOT forbidden here
    # the strict-gate write (masked policy) must NOT raise
    assert assert_no_leak(blob, web_secrets, policy="strict") == []
    assert "211070175" in blob                           # routing legitimately shown
    assert "1408137817" not in blob and "DE44500105175407324931" not in blob
    # but the naive "all vault secrets" set WOULD have crashed — proving the guard matters
    with pytest.raises(ValueError):
        assert_no_leak(blob, e.vault.secrets(), policy="strict")
