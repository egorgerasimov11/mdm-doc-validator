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
