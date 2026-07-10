"""G1: the account_holder role gate — a signatory/contact-labeled name is the
SIGNER, not the owner (real Mercury case: 'Account signatory: <person>' became
the holder and the run ACCEPTed with confidence high)."""
from mdmdoc import stage_b
from mdmdoc.fields import Extraction
from mdmdoc.stage_a import RawDoc

MERCURY = ("Mercury\nThis letter is to verify that Jamcorder LLC is a customer "
           "of Mercury.\nAccount details\nAccount number: 202412345678\n"
           "Account signatory\nCharles A. Fakeperson\n")


def _run(holder, text):
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_holder": holder}
    raw = RawDoc(path="x.pdf", sha256="e" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = text
    stage_b._ground_account_holder(ext, raw)
    return ext


def test_signatory_labeled_value_moves():
    ext = _run("Charles A. Fakeperson", MERCURY)
    assert ext.fields["account_signatory"] == "Charles A. Fakeperson"
    assert ext.fields["account_holder"] == "Jamcorder LLC"   # relationship rescue
    assert ext.provenance["account_holder"]["source"] == "rule"


def test_holder_labeled_value_kept():
    text = "Account holder\nJamcorder LLC\nAccount signatory\nSomeone Else\n"
    ext = _run("Jamcorder LLC", text)
    assert ext.fields["account_holder"] == "Jamcorder LLC"
    assert "account_signatory" not in ext.fields


def test_relationship_rescue_verify_that():
    ext = _run("", "We hereby confirm that Fake Trading GmbH is a client of Fakebank.")
    assert ext.fields["account_holder"] == "Fake Trading GmbH"


def test_relationship_rescue_holds_account():
    ext = _run("", "Fake Trading GmbH maintains a business checking account with us.")
    assert ext.fields["account_holder"] == "Fake Trading GmbH"


def test_no_context_no_move():
    ext = _run("Plain Value Corp", "Totally unrelated text without the name printed.")
    assert ext.fields["account_holder"] == "Plain Value Corp"


def test_non_bank_untouched():
    ext = Extraction(doc_class="w9", doc_type="w9")
    ext.fields = {"account_holder": "X"}
    raw = RawDoc(path="x.pdf", sha256="e" * 16, ext=".pdf", doc_class="w9")
    raw.raw_text = MERCURY
    stage_b._ground_account_holder(ext, raw)
    assert ext.fields["account_holder"] == "X"
