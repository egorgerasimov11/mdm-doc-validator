import pytest

from mdmdoc.privacy import (SecretVault, assert_no_leak, fake_preserve_shape, mask,
                            scrub_text)


def test_mask_hyphen_styles_preserved():
    assert mask("ssn", "320-54-0693") == "XXX-XX-0693"
    assert mask("ein", "12-3456789") == "XX-XXX6789"
    assert mask("tin", "320-54-0693") == "XXX-XX-0693"     # routed by shape
    assert mask("tin", "12-3456789") == "XX-XXX6789"
    assert mask("iban", "DE44 5001 0517 5407 3249 31") == "DE**…4931"
    assert mask("account_number", "1830042757") == "…2757"


def test_mask_tin_keeps_a_trailing_letter():
    # a CN taxpayer id / unified-credit code ends in a check LETTER; a
    # digit-only tail used to drop it (…0233T -> …0233), making the value
    # look truncated (H1 bug 2). The letter is not sensitive.
    m = mask("tin", "91110105674250233T")
    assert m.endswith("233T") and "91110105674250233" not in m
    # a plain digit tin is unchanged
    assert mask("tin", "12345678901234567") == "*" * 13 + "4567"


def test_fake_preserves_shape_and_is_deterministic():
    v = "DE44500105175407324931"
    f1 = fake_preserve_shape("iban", v)
    f2 = fake_preserve_shape("iban", v)
    assert f1 == f2
    assert len(f1) == len(v)
    assert f1[:2] == "DE"
    assert f1 != v
    e = fake_preserve_shape("ein", "12-3456789")
    assert e[2] == "-" and len(e) == 10 and e != "12-3456789"


def test_scrub_text_masks_patterns_and_known_values():
    vault = SecretVault()
    vault.register("account_number", "1830042757")
    txt = ("IBAN DE44500105175407324931, SSN 320-54-0693, EIN 12-3456789, "
           "account 1830042757 and spaced 18 3004 2757 end")
    out = scrub_text(txt, vault)
    assert "DE44500105175407324931" not in out
    assert "320-54-0693" not in out
    assert "12-3456789" not in out
    assert "1830042757" not in out
    assert "3004 2757" not in out
    assert "XXX-XX-0693" in out


def test_assert_no_leak_blocks_and_passes():
    with pytest.raises(ValueError):
        assert_no_leak("the ssn is 320-54-0693", [])
    with pytest.raises(ValueError):
        assert_no_leak("iban DE44500105175407324931", [])
    with pytest.raises(ValueError):
        assert_no_leak("account 1830 0427 57", ["1830042757"])
    # masked forms must pass
    assert assert_no_leak("XXX-XX-0693 DE**…4931 …2757 XX-XXX6789", ["1830042757"]) == []


def test_vault_sensitive_map_has_no_real_values():
    vault = SecretVault()
    vault.register("iban", "DE44500105175407324931")
    m = vault.sensitive_map()
    assert m and "value" not in m[0]
    assert m[0]["masked"].startswith("DE**")
    assert m[0]["fake"] != "DE44500105175407324931"
