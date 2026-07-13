"""oplog opaque-id guard: a uuid4 hex job_id that comes out all-digit and 11-18
chars long collides with the strict numeric leak pattern (\\b\\d{11,18}\\b) in the
eval leak sweep — a false positive on an id, not data. _safe_id prefixes a stable
marker so the token can never be read as a bare long number, deterministically."""
from mdmdoc import oplog


def test_all_digit_long_id_is_prefixed():
    assert oplog._safe_id("849370017107") == "x849370017107"   # 12 all-digit -> guarded


def test_hex_id_with_letters_untouched():
    assert oplog._safe_id("326b0f79bb69") == "326b0f79bb69"     # normal hex -> as-is


def test_short_numeric_id_untouched():
    assert oplog._safe_id("12345678") == "12345678"             # 8 digits < 11 -> safe


def test_deterministic_pairs_still_match():
    jid = "999088401234"
    assert oplog._safe_id(jid) == oplog._safe_id(jid)           # job-start == job-end


def test_log_writes_guarded_job_id(tmp_path, monkeypatch):
    from mdmdoc import config
    monkeypatch.setattr(config, "DATASET_DIR", tmp_path)
    row = oplog.log("job-start", job_id="700000017107")
    assert row["job_id"] == "x700000017107"
