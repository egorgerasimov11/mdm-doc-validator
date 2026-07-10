"""V-wave: the tax case — worldwide catalog, wrong-country detector, US
doctrine, masks, duplicates. All values invented."""
from mdmdoc.bulk import tax, taxmath


def _row(cat, num, partner="1", long="", country=""):
    return {"partner": partner, "tax_category": cat, "tax_number": num,
            "tax_number_long": long, "country": country}


def _one(cat, num, **kw):
    return tax.check_rows([_row(cat, num, **kw)])[0]


def test_de_vat_in_us_category_is_wrong_country():
    """The real finding this feature was built around."""
    rv = _one("US0", "DE137196337")
    assert rv.bucket == "INVALID" and "BULK-T03" in rv.rule_ids
    assert "German VAT" in rv.reasons[0]


def test_us_doctrine_structure_decides_not_category():
    assert _one("US1", "12-3456780").bucket == "VALID"     # EIN shape in 'SSN' cat
    assert _one("US4", "223-45-6789").bucket == "VALID"    # SSN shape in custom cat
    rv = _one("US2", "000-12-3456")                        # invalid everywhere
    assert rv.bucket == "INVALID" and "BULK-T02" in rv.rule_ids
    rv = _one("US1", "123456789")                          # sequential fake
    assert rv.bucket == "INVALID" and "fake" in rv.reasons[0]


def test_valid_national_numbers():
    assert _one("DE0", "DE811907980").bucket == "VALID"
    assert _one("IT0", "00743110157").bucket == "VALID"
    assert _one("BE0", "0417497106").bucket == "VALID"
    assert _one("AU0", "51824753556").bucket == "VALID"


def test_checksum_failures():
    rv = _one("DE0", "DE811907981")                        # last digit wrong
    assert rv.bucket == "INVALID" and "BULK-T04" in rv.rule_ids
    rv = _one("PL0", "1234567890")
    assert rv.bucket == "INVALID"


def test_masked_and_empty_skip():
    assert _one("US4", "XXXXXXX").bucket == "SKIPPED"
    assert _one("US4", "XXXXXXX").rule_ids == ["BULK-T05"]
    assert _one("DE0", "").bucket == "SKIPPED"
    assert _one("DE0", "").rule_ids == ["BULK-T07"]


def test_unknown_category_suspicious():
    rv = _one("QQ9", "12345")
    assert rv.bucket == "SUSPICIOUS" and "BULK-T01" in rv.rule_ids


def test_duplicates_across_partners():
    rows = [_row("US2", "12-3456789", partner="A"),
            _row("US2", "12-3456789", partner="B"),
            _row("US2", "98-7654321", partner="C")]
    out = tax.check_rows(rows)
    assert "BULK-T06" in out[0].rule_ids and "BULK-T06" in out[1].rule_ids
    assert "BULK-T06" not in out[2].rule_ids


def test_country_column_mismatch():
    rv = _one("DE0", "DE811907980", country="US")
    assert "BULK-T08" in rv.rule_ids and rv.bucket == "SUSPICIOUS"


def test_tax_number_long_wins():
    rv = tax.check_rows([_row("DE0", "junk", long="DE811907980")])[0]
    assert rv.bucket == "VALID"


def test_taxmath_registry_all_callable():
    for name, fn in taxmath.REGISTRY.items():
        ok, detail = fn("")
        assert isinstance(ok, bool) and isinstance(detail, str), name
