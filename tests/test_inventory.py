"""O2: the document inventory — labeled identifiers outside the schema are
captured, masked and surfaced; two DIFFERENT labeled accounts raise the
distinct_accounts flag (BNK-031); a repeated one corroborates. Root case: a
Chinese letter's tax ID/phone/address/second account block vanished silently,
and the 18-char CN taxpayer ID was not even leak-gated."""
from mdmdoc import inventory, stage_b
from mdmdoc.fields import Extraction
from mdmdoc.rules.engine import run_rules
from mdmdoc.stage_a import RawDoc

CN_DOC = ("银行账户信息\n我公司发票信息如下:\n公司名称: 假冒服务有限公司\n"
          "纳税人识别号: 91110105674299999T\n"
          "地址: 北京市东城区某某胡同甲 1 号\n电话: 010-56060000\n"
          "开户行:中国某某银行股份有限公司北京支行\n账号: 35310188000049999\n"
          "我公司银行信息如下(收付款账户):\n"
          "开户银行名称:中国某某银行股份有限公司北京支行\n账号: 35310188000049999\n"
          "联行号: 303100000999\n")


def _run(text=CN_DOC, fields=None):
    ext = Extraction(doc_class="bank", doc_type="payment_instructions")
    ext.fields = {"account_number": "35310188000049999", **(fields or {})}
    raw = RawDoc(path="cn.pdf", sha256="0" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = text
    stage_b._collect_inventory(ext, raw)
    return ext


def test_families_captured():
    items = inventory.collect(CN_DOC)
    fams = {i["family"] for i in items}
    assert {"tax_id", "account", "clearing", "phone", "address"} <= fams
    accounts = [i for i in items if i["family"] == "account"]
    assert len(accounts) >= 2                      # both blocks kept


def test_cn_tax_id_masked_and_vaulted():
    ext = _run()
    tax = next(i for i in ext.inventory if i["family"] == "tax_id")
    assert "91110105674299999" not in tax["value"]      # masked in the artifact
    assert any("91110105674299999T" == s for s in ext.vault.secrets())


def test_corroboration_note_for_repeated_account():
    ext = _run()
    assert "distinct_accounts" not in ext.fields
    assert any("corroborated by 2 labeled occurrences" in n for n in ext.crosscheck)


def test_distinct_accounts_flag_and_rule():
    text = CN_DOC.replace("账号: 35310188000049999\n联行号",
                          "账号: 62220202000012399\n联行号", 1)
    ext = _run(text=text, fields={"account_holder": "假冒服务有限公司",
                                  "bank_name": "某某银行", "signed": True})
    assert ext.fields["distinct_accounts"] is True
    ids = {f.rule_id for f in run_rules(ext, enforce_approvals=False)}
    assert "BNK-031" in ids


def test_iban_plus_konto_is_not_distinct():
    text = ("Bankbestaetigung\nIBAN: DE89 3704 0044 0532 0130 00\n"
            "Kontonummer: 532013000\n")
    ext = _run(text=text, fields={"account_number": "532013000",
                                  "iban": "DE89370400440532013000"})
    assert "distinct_accounts" not in ext.fields


def test_inventory_persists_masked_in_public_view():
    ext = _run()
    pub = ext.to_public(policy="masked")
    inv = pub.get("inventory") or []
    assert inv, "inventory must reach the public artifact"
    joined = " ".join(i["value"] for i in inv)
    assert "91110105674299999" not in joined


def test_scan_path_prefers_cjk_vision_text():
    """Scans: tesseract mangles hanzi into latin junk that wins the realword
    count — the inventory must read the vision transcription instead."""
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_number": "35310188000049999"}
    raw = RawDoc(path="scan.pdf", sha256="1" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = "meRARIS: 91110105674299999T mS: 35310188000049999 RTS: 303100000999"
    raw.vision_text = CN_DOC
    stage_b._collect_inventory(ext, raw)
    fams = {i["family"] for i in ext.inventory}
    assert "tax_id" in fams and "clearing" in fams


# ---------------------------------------------------------------- G-wave -----
# Root case (Altium remit form, run 7a7539c1 on the mini): v1 matched labels
# without word boundaries and let (?i) leak into value classes — the page
# showed "Account | s Receivable", "Address | for 4225 Executive…", dropped
# FEIN/DUNS/e-mails entirely, silently lost the 10-digit wires routing and the
# second SWIFT with its printed qualifiers. Fixture mirrors that OCR line
# skeleton with INVENTED identifiers.
US_REMIT = """AcmeCo LLC - Company Information
AcmeCo LLC - Office Address
Company Name AcmeCo LLC
Mailing Address for 1 Example Square, Suite# 8
City Springfield
State CA
Zip Code 00000
Company's MAIN Phone Number 555-000-1111
Fax Number No Longer Available
FEIN Number 00-0000000
Duns Number 00-000-00000
REMIT PAYMENT INFORMATION
Name on Account AcmeCo LLC
Bank Name First Example Bank - Checks not to be mailed here
Bank Address 100 N. Example St..
City, State, Zip Code Springfield, CA. 00000
Account Number 001200000000
Routing for ACH 121000358
Swift Code BOFAUS3N US Domestic / BOFAUS6S Foreign Currency
Routing for Wires 0260095933
Payment Currency USD
AcmeCo LLC - Accounts Receivable / U.S. Finance - Credit Card Payment Processing / Purchase Orders
E-mail Address for EFT payment notification pay@example.com
AcmeCo LLC - Accounting Admin - Tax issues/Inquiries / Portal Admin - Submission/Retrieval/forms/Invites
Tel: 555-000-1111 1 Example Square, Suite 8, Springfield CA 00000, United States www.example.com
"""


def _run_us(fields=None):
    from mdmdoc import ocr
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_holder": "AcmeCo LLC", "swift_bic": "BOFAUS3N",
                  "account_number": "001200000000", "routing_aba": "121000358",
                  **(fields or {})}
    raw = RawDoc(path="us.pdf", sha256="1" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = US_REMIT
    raw.regex_candidates = ocr.regex_fields(US_REMIT)
    stage_b._ground_bank_address(ext, raw)
    stage_b._annotate_bank_ids(ext, raw)
    stage_b._collect_inventory(ext, raw)
    return ext


def test_no_midword_garbage_rows():
    """'Accounts Receivable' / 'Accounting Admin' must never yield account
    rows; every value starts with a real token, not a word fragment."""
    rows = inventory.collect(US_REMIT)
    assert not any(v.startswith(("s ", "ing ", "for ")) for v in
                   (r["value"] for r in rows)), rows
    acc = [r for r in rows if r["family"] == "account"]
    assert [r["value"] for r in acc] == ["001200000000"]


def test_us_remit_families_complete():
    rows = inventory.collect(US_REMIT)
    fam = {r["family"]: True for r in rows}
    for want in ("tax_id", "company_reg", "holder", "account", "routing",
                 "swift", "phone", "email", "city_state_zip", "currency",
                 "address", "other"):
        assert want in fam, f"family {want} missing"
    by_label = {r["label"]: r["value"] for r in rows}
    assert by_label["FEIN Number"] == "00-0000000"
    assert by_label["Duns Number"] == "00-000-00000"
    assert by_label["Mailing Address for"] == "1 Example Square, Suite# 8"
    assert by_label["City, State, Zip Code"] == "Springfield, CA. 00000"
    assert by_label["Payment Currency"] == "USD"
    assert by_label["Fax Number"] == "No Longer Available"
    # footer phone must not swallow the street number into the phone value
    assert by_label["Tel"] == "555-000-1111"
    # e-mail behind a long label tail is still caught
    assert any(r["value"] == "pay@example.com" for r in rows)


def test_second_swift_and_qualifiers_kept():
    rows = inventory.collect(US_REMIT)
    swifts = {r["value"]: r["label"] for r in rows if r["family"] == "swift"}
    assert "BOFAUS3N" in swifts and "US Domestic" in swifts["BOFAUS3N"]
    assert "BOFAUS6S" in swifts and "Foreign Currency" in swifts["BOFAUS6S"]


def test_wires_routing_surfaces_with_note():
    """The 10-digit labeled wires value never reaches a routing field but is
    NOT silently dropped: inventory row + note + crosscheck."""
    ext = _run_us()
    assert not str(ext.fields.get("routing_aba_wires") or "")
    row = next(r for r in ext.inventory
               if r["family"] == "routing" and "Wires" in r["label"])
    assert "not a valid US ABA" in row.get("note", "")
    assert any("NOT written to the routing field" in c for c in ext.crosscheck)


def test_matches_tags_and_masking():
    ext = _run_us()
    tagged = {r["label"]: r.get("matches") for r in ext.inventory}
    assert tagged.get("Account Number") == "account_number"
    assert tagged.get("Routing for ACH") == "routing_aba"
    assert tagged.get("Name on Account") == "account_holder"
    fein = next(r for r in ext.inventory if r["label"] == "FEIN Number")
    assert "0000000" not in fein["value"]          # tin-kind is masked
    assert "00-0000000" in ext.vault.secrets()     # …and leak-gated
    assert "0260095933" in ext.vault.secrets()     # the suspect routing too


def test_ground_bank_address_fills_and_never_overwrites():
    ext = _run_us()
    assert ext.fields["bank_address"] == "100 N. Example St, Springfield, CA. 00000"
    assert ext.provenance["bank_address"]["source"] == "ocr-regex"
    assert any("bank_address=filled" in c for c in ext.crosscheck)
    ext2 = _run_us(fields={"bank_address": "1 Model Street"})
    assert ext2.fields["bank_address"] == "1 Model Street"


def test_swift_qualifier_field_and_report_hint():
    ext = _run_us()
    assert ext.fields.get("swift_qualifier") == "US Domestic"
    assert any("second SWIFT on the document: BOFAUS6S (Foreign Currency)" in c
               for c in ext.crosscheck)
    from mdmdoc import report
    pub = ext.to_public(policy="full")
    rows = {r[3]: r for r in report._data_rows(pub)}
    assert "(US Domestic)" in rows["swift_bic"][1]
    assert rows["routing_aba"][2] == "ACH payment method"
    assert rows["routing_aba_wires"][2] == "wire transfers"


def test_other_digit_runs_never_fake_two_accounts():
    """A long digit run under an unknown 'other' label is masked like an
    account but must NOT feed the distinct_accounts signal."""
    text = ("Account Number 001200000000\n"
            "Reference Number 9988776655443\n")
    ext = Extraction(doc_class="bank", doc_type="bank_letter")
    ext.fields = {"account_number": "001200000000"}
    raw = RawDoc(path="x.pdf", sha256="2" * 16, ext=".pdf", doc_class="bank")
    raw.raw_text = text
    stage_b._collect_inventory(ext, raw)
    assert not ext.fields.get("distinct_accounts")
    ref = next(r for r in ext.inventory if r["label"] == "Reference Number")
    assert "9988776655443" not in ref["value"]     # policy-masked display
