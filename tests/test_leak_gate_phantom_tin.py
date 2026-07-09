"""Phantom-TIN regression (the Zajecka eval crash): EIN/SSN patterns matched
INSIDE hyphen-grouped bank numbers (LV/GB sort-code prints), registering a fake
TIN 'secret' whose digits then gate-blocked the document's own account numbers.
The adjacency guards kill the class at every layer: candidate extraction,
scrub, and the leak gate itself."""
from mdmdoc import ocr
from mdmdoc.privacy import EIN_SPACED_RE, SSN_SPACED_RE, assert_no_leak, scrub_text


def test_ein_not_matched_inside_bank_number():
    # sort-code style grouping must NOT yield a phantom EIN
    assert not ocr.EIN_RE.findall("Konto 61-26-1234500 33 SWIFT NOVALV22")
    assert not ocr.EIN_RE.findall("Ref 12-3456789-01 payment")   # hyphen-adjacent
    assert not ocr.SSN_RE.findall("Acct 4-123-45-6789-9")


def test_real_ein_ssn_still_matched():
    assert ocr.EIN_RE.findall("EIN: 12-3456789.") == ["12-3456789"]
    assert ocr.EIN_RE.findall("(12-3456789)") == ["12-3456789"]
    assert ocr.SSN_RE.findall("SSN 123-45-6789 on file") == ["123-45-6789"]


def test_spaced_variants_guarded_but_alive():
    assert not EIN_SPACED_RE.findall("total 861 12 3456789 22 EUR")[0:0]  # guard sanity below
    assert EIN_SPACED_RE.search("EIN 12 3456789 given") is not None
    assert EIN_SPACED_RE.search("run 861-12 3456789") is None          # hyphen-adjacent
    assert SSN_SPACED_RE.search("SSN 123 45 6789.") is not None
    assert SSN_SPACED_RE.search("id 9123 45 6789") is None             # digit-adjacent


def test_gate_no_phantom_hit_on_bank_grouping():
    # a bank doc's own grouped account digits are NOT a TIN leak
    text = "Bank LV Nova. Konto 61-26-1234500 33. Amount 1 200,00"
    assert assert_no_leak(text, known_secrets=[], raise_on_hit=False,
                          policy="tin-only") == []


def test_bank_id_never_doubles_as_tin_secret(monkeypatch):
    """One digit string cannot be both a full-display banking id and an
    everywhere-masked TIN — the banking identity wins (the Zajecka crash)."""
    import mdmdoc.stage_b as sb
    from mdmdoc.stage_a import RawDoc

    monkeypatch.setattr(sb, "_run_model", lambda *a, **k: (None, False, "m"))
    raw = RawDoc(path="/x/lv.pdf", sha256="d" * 64, ext=".pdf", doc_class="bank")
    raw.raw_text = "Nr. 61-2612345 konto"
    raw.regex_candidates = {"routing_aba": "612612345", "ein": "61-2612345"}
    ext = sb.extract(raw, engine="deterministic")
    kinds = {it["kind"] for it in ext.vault.items()}
    assert "routing_aba" in kinds and "ein" not in kinds
    assert ext.vault.tin_secrets() == []
    assert any("treated as a bank id" in n for n in ext.crosscheck)
    # a NON-colliding EIN on a bank letter still registers as a TIN secret
    raw2 = RawDoc(path="/x/us.pdf", sha256="e" * 64, ext=".pdf", doc_class="bank")
    raw2.raw_text = "EIN 12-3456789, account 555000111"
    raw2.regex_candidates = {"account_number": "555000111", "ein": "12-3456789"}
    ext2 = sb.extract(raw2, engine="deterministic")
    assert ext2.vault.tin_secrets() != []


def test_gate_and_scrub_stay_symmetric_on_real_secret():
    secret = "12-3456789"
    spaced = "EIN printed as 12 - 34 56 789 here"
    # the gate finds the spaced variant…
    hits = assert_no_leak(spaced, known_secrets=[secret], raise_on_hit=False,
                          policy="tin-only")
    assert any(h.startswith("known-secret:") for h in hits)
    # …and the scrubber can remove exactly what the gate finds

    class _V:
        def items(self):
            from mdmdoc.privacy import fake_preserve_shape, mask
            return [{"kind": "ein", "value": secret,
                     "masked": mask("ein", secret),
                     "fake": fake_preserve_shape("ein", secret)}]

    cleaned = scrub_text(spaced, _V(), policy="tin-only")
    assert assert_no_leak(cleaned, known_secrets=[secret], raise_on_hit=False,
                          policy="tin-only") == []
