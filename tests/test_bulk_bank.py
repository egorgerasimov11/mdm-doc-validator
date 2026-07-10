"""V-wave: the bank case — bankmath reuse, IBAN/SWIFT, national key shapes,
duplicates, and the mocked web layer with its cache."""
import json

from mdmdoc.bulk import bank, webcheck


def _row(**kw):
    base = {"partner": "1", "bank_country": "US", "bank_key": "021000021",
            "bank_account": "12345678", "iban": "", "swift_bic": "",
            "control_key": "", "account_holder": "ACME"}
    base.update(kw)
    return base


def _one(**kw):
    return bank.check_rows([_row(**kw)])[0]


def test_valid_us_row():
    assert _one().bucket == "VALID"


def test_aba_checksum_and_prefix():
    rv = _one(bank_key="123456789")
    assert rv.bucket == "INVALID" and "BULK-B02" in rv.rule_ids
    rv = _one(bank_key="130000006")               # checksum-valid, 13 prefix dead
    assert "BULK-B03" in rv.rule_ids


def test_bank_key_format():
    rv = _one(bank_key="2100002")
    assert "BULK-B01" in rv.rule_ids
    rv = _one(bank_key="DEUTDEFFXXX")
    assert "BULK-B01" in rv.rule_ids and "SWIFT" in rv.reasons[0]


def test_account_zeros_and_padding():
    assert _one(bank_account="000000000000").bucket == "INVALID"
    rv = _one(bank_account="000000000012")
    assert rv.bucket == "SUSPICIOUS" and "BULK-B04" in rv.rule_ids
    assert _one(bank_account="000001234567").bucket == "VALID"   # padding ok


def test_iban_checks():
    rv = _one(bank_country="DE", bank_key="37040044",
              iban="DE89370400440532013000")
    assert rv.bucket == "VALID"
    rv = _one(bank_country="DE", bank_key="37040044",
              iban="DE89370400440532013001")
    assert "BULK-B05" in rv.rule_ids
    rv = _one(bank_country="FR", bank_key="", bank_account="123456",
              iban="DE89370400440532013000")
    assert "BULK-B06" in rv.rule_ids


def test_swift_and_national_shapes():
    rv = _one(swift_bic="NOT-A-BIC")
    assert "BULK-B07" in rv.rule_ids
    rv = _one(bank_country="IN", bank_key="HDFC0001234", bank_account="5551234")
    assert rv.bucket == "VALID"
    rv = _one(bank_country="IN", bank_key="12345", bank_account="5551234")
    assert "BULK-B10" in rv.rule_ids
    rv = _one(bank_country="CN", bank_key="303100000397", bank_account="5551234")
    assert rv.bucket == "VALID"                    # CNAPS 12 digits


def test_control_key_and_duplicates():
    rv = _one(control_key="99")
    assert "BULK-B08" in rv.rule_ids
    rows = [_row(partner="A"), _row(partner="B")]
    out = bank.check_rows(rows)
    assert all("BULK-B09" in r.rule_ids for r in out)


def test_masked_and_empty_skip():
    assert _one(bank_account="", iban="").bucket == "SKIPPED"
    assert _one(bank_account="XXXXXX").bucket == "SKIPPED"


def test_web_layer_mocked_and_cached(tmp_path, monkeypatch):
    from mdmdoc import config
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path)
    calls = []

    class FakeEv:
        def __init__(self, status, label):
            self.status, self.label, self.source_url = status, label, "http://x"

    def fake_dir(aba, vault):
        calls.append(aba)
        return FakeEv("not_found" if aba == "061112788" else "found",
                      f"routing {aba} looked up")

    import mdmdoc.web_enrichment.aba as aba_mod
    monkeypatch.setattr(aba_mod, "_directory_evidence", fake_dir)
    monkeypatch.setattr("time.sleep", lambda s: None)

    rows = [_row(partner="A"), _row(partner="B", bank_key="061112788"),
            _row(partner="C", bank_key="badbadbad")]
    notes: list = []
    out = bank.check_rows(rows, web=True, notes=notes)
    assert "BULK-B12" in out[1].rule_ids           # not_found -> suspicious
    assert any("looked up" in r for r in out[0].reasons)   # found -> cited
    assert calls.count("021000021") == 1           # unique lookups only
    cache = json.loads((tmp_path / webcheck.CACHE_NAME).read_text())
    assert cache["021000021"]["status"] == "found"
    calls.clear()
    bank.check_rows(rows, web=True, notes=[])
    assert calls == []                             # second run fully cached
