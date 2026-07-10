#!/usr/bin/env python3
"""
synth.py — deterministic SYNTHETIC eval corpus with known ground truth (П1).

The real labeled corpus is 18 PII documents living only on the mini — every
verdict metric on it carries a ±5.5pp/doc swing (Wilson CI spans half the unit
interval). This module generates a PII-FREE corpus of realistic text-layer PDFs
(invented entities, mod-97-valid fake IBANs, EIN 00-…/SSN 000-… reserved-range
fakes) whose TRUTH is known by construction, as a SEPARATE eval stratum:

  eval/synthetic/docs/*.pdf     the documents (committed — reproducible)
  eval/synthetic/labels.jsonl   labels in the real labels.jsonl schema
                                (+ source:"synthetic", generator metadata)
  eval/synthetic/MANIFEST.json  seed, version, dimension matrix

Two truths per doc:
  verdict_gold   what the APPROVED RULES say about the TRUE fields (computed by
                 the real engine on the truth extraction — never hand-coded);
  det_expected   what today's deterministic pipeline actually produces on the
                 PDF (perceive→extract→rules→decide) — the CI regression lock.

The headline 18-doc metrics NEVER mix with this stratum (evalrun writes
synthetic_* artifacts); load_labels() never sees synthetic rows, so few-shot /
LoRA / precedents structurally cannot train on synthetic data.
"""
from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

from . import config

GENERATOR_VERSION = 2
DEFAULT_SEED = 20260709

# --- fake identities ---------------------------------------------------------
_COMPANIES = ["Vela s.r.o.", "Norwind ApS", "Helios OU", "Kestrel BV",
              "Meridian Ltd", "Altair GmbH", "Fuente Azul SL", "Quercus Sp. z o.o.",
              "Baltika SIA", "Rowan Partners LLC", "Cinder Labs Inc", "Tovarna Nova d.o.o."]
_BANKS = {"DE": ("Commerzbank AG", "COBADEFFXXX"), "GB": ("NatWest Bank", "NWBKGB2LXXX"),
          "NL": ("ABN AMRO", "ABNANL2AXXX"), "ES": ("Banco Cielo", "CIELESMMXXX"),
          "CZ": ("Ceska sporitelna", "GIBACZPXXXX"), "FR": ("La Banque Postale", "PSSTFRPPXXX")}
_PEOPLE = ["Jonas Merel", "Clara Voss", "Tomas Ridley", "Elena Marsh",
           "Viktor Hale", "Ana Petri"]

# BBAN shapes (digits unless noted). Lengths per the shipped iban_length table.
_BBAN = {"DE": 18, "NL": ("ABNA", 10), "ES": 20, "CZ": 20, "FR": 23, "GB": ("NWBK", 14)}


def _mod97_check(cc: str, bban: str) -> str:
    """ISO 13616 check digits so BNK-011 sees the fake as VALID."""
    def num(s: str) -> str:
        return "".join(str(int(ch, 36)) for ch in s)
    rem = int(num(bban + cc + "00")) % 97
    return f"{98 - rem:02d}"


def synth_iban(cc: str, rng: random.Random) -> str:
    shape = _BBAN[cc]
    if isinstance(shape, tuple):
        bank, ndig = shape
        bban = bank + "".join(rng.choice("0123456789") for _ in range(ndig))
    else:
        bban = "".join(rng.choice("0123456789") for _ in range(shape))
    return cc + _mod97_check(cc, bban) + bban


def break_checksum(iban: str) -> str:
    """Flip one BBAN digit so mod-97 fails (verified by the caller)."""
    from .fields import iban_mod97_ok
    for i in range(len(iban) - 1, 3, -1):
        if iban[i].isdigit():
            cand = iban[:i] + str((int(iban[i]) + 1) % 10) + iban[i + 1:]
            if not iban_mod97_ok(cand):
                return cand
    raise ValueError("could not break checksum")


def synth_ein(rng: random.Random) -> str:
    return f"00-{rng.randrange(1_000_000, 9_999_999)}"       # 00- prefix: never assigned


def synth_ssn(rng: random.Random) -> str:
    return f"000-{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}"  # 000 area: invalid


# --- document text templates ---------------------------------------------------
_LETTER = {
    "en": ["{bank}", "Bank confirmation letter", "Date: {date}",
           "To whom it may concern,",
           "We confirm that {holder} holds the following account with our bank:",
           "IBAN: {iban}", "BIC: {bic}", "{signoff}"],
    "de": ["{bank}", "Bankbestaetigung", "Datum: {date}", "Sehr geehrte Damen und Herren,",
           "hiermit bestaetigen wir, dass {holder} das folgende Konto bei uns fuehrt:",
           "IBAN: {iban}", "BIC: {bic}", "{signoff}"],
    "es": ["{bank}", "Certificacion bancaria", "Fecha: {date}", "A quien corresponda:",
           "Confirmamos que {holder} mantiene la siguiente cuenta en nuestro banco:",
           "IBAN: {iban}", "BIC: {bic}", "{signoff}"],
}
_SIGNOFF = {
    "esig": "DocuSign Envelope ID: {tag}. Electronically signed.",
    "typed": "This is a computer generated confirmation and requires no signature.\n"
             "Officer block: {person}, Account Services.",
    "unsigned": "Sincerely, {bank} Account Services.",
}
_STATEMENT = ["{bank}", "Bank statement for account holder {holder}",
              "IBAN {iban}", "Statement period 1 Jan 2026 to 31 Mar 2026",
              "Opening balance 1,000.00  Closing balance 1,200.00"]
_PAYMENT = ["{holder}", "Supplier payment instructions",
            "Please remit all future payments to:",
            "Beneficiary: {holder}", "IBAN: {iban}", "Bank: {bank}",
            "Standard settlement instructions — ACH only."]
_INVOICE = ["INVOICE", "Invoice Number INV-{num}", "Invoice date {date}",
            "Bill to: {holder}", "Total due {amount} EUR",
            "Remit to IBAN {iban}"]
_LETTERHEAD = ["{holder}", "Official supplier letterhead",
               "Banking details for vendor payments:",
               "Account holder: {holder}", "IBAN: {iban}", "Bank: {bank}",
               "Authorized: {person} (signature on file)"]
_VOIDED = ["VOID VOID VOID", "{holder}", "{bank}",
           "Routing: 021000021  Account: {acct}", "Check No. 1001 — VOIDED"]
_W9_HEAD = ["Form W-9", "Request for Taxpayer Identification Number and Certification"]


def _w9_text(rng: random.Random, holder: str, tin_kind: str, boxed: bool,
             classification: str, signed: bool, dba: str = "",
             tin_value: str = "") -> tuple[str, dict]:
    # Always draw, then override: the rng stream length stays invariant, so a
    # crafted tin_value never shifts the bytes of any doc generated after it.
    tin = synth_ein(rng) if tin_kind == "EIN" else synth_ssn(rng)
    if tin_value:
        tin = tin_value
    lines = list(_W9_HEAD)
    lines.append(f"1 Name (as shown on your income tax return): {holder}")
    lines.append(f"2 Business name/disregarded entity name: {dba}")
    lines.append(f"3 Federal tax classification: {classification}")
    lines.append("5 Address: 12 Harbor Lane")
    lines.append("6 City, state, ZIP: Springfield, IL 62701")
    label = ("Employer identification number" if tin_kind == "EIN"
             else "Social security number")
    if boxed:
        lines.append(label)
        lines += list(tin.replace("-", ""))
    else:
        lines.append(f"{label}: {tin}")
    if signed:
        lines.append("Signature of U.S. person: (signed)  Date: 02/11/2026")
    truth = {"line1_name": holder, "line2_business_name": dba,
             "line3_classification": classification,
             "tin_type": tin_kind, "tin_raw": tin,
             "address_street": "12 Harbor Lane",
             "address_city_state_zip": "Springfield, IL 62701",
             "signed": signed, "sign_date": "02/11/2026" if signed else ""}
    return "\n".join(lines), truth


def _spec_list(rng: random.Random) -> list[dict]:
    """The curated dimension matrix -> one spec per document."""
    specs: list[dict] = []
    ccs = list(_BANKS)

    def bank_fields(cc, holder, signed, evidence=""):
        bank, bic = _BANKS[cc]
        return {"account_holder": holder, "bank_name": bank, "bank_country": cc,
                "swift_bic": bic, "signed": signed,
                **({"signature_evidence": evidence} if evidence else {})}

    i = 0
    # bank letters: 3 languages x 3 signature modes x {valid, invalid} IBAN
    for lang in ("en", "de", "es"):
        for sig in ("esig", "typed", "unsigned"):
            for validity in ("valid", "invalid"):
                cc = ccs[i % len(ccs)]
                holder = _COMPANIES[i % len(_COMPANIES)]
                specs.append({"template": "letter", "lang": lang, "sig": sig,
                              "validity": validity, "cc": cc, "holder": holder})
                i += 1
    # letters with the holder line MISSING (BNK-023 truth)
    for lang in ("en", "de", "es"):
        cc = ccs[i % len(ccs)]
        specs.append({"template": "letter_no_holder", "lang": lang, "sig": "typed",
                      "validity": "valid", "cc": cc, "holder": ""})
        i += 1
    # statements: holder present / missing
    for variant in ("holder", "no_holder", "holder2", "no_holder2"):
        cc = ccs[i % len(ccs)]
        holder = "" if variant.startswith("no") else _COMPANIES[i % len(_COMPANIES)]
        specs.append({"template": "statement", "lang": "en", "cc": cc,
                      "holder": holder, "validity": "valid", "sig": "unsigned"})
        i += 1
    # payment instructions / invoices / letterhead / voided check
    for k in range(2):
        cc = ccs[i % len(ccs)]
        specs.append({"template": "payment", "lang": "en", "cc": cc,
                      "holder": _COMPANIES[i % len(_COMPANIES)],
                      "validity": "valid", "sig": "unsigned"})
        i += 1
    for lang in ("en", "de", "es"):
        cc = ccs[i % len(ccs)]
        specs.append({"template": "invoice", "lang": lang, "cc": cc,
                      "holder": _COMPANIES[i % len(_COMPANIES)],
                      "validity": "valid", "sig": "unsigned"})
        i += 1
    for k in range(3):
        cc = ccs[i % len(ccs)]
        specs.append({"template": "letterhead", "lang": "en", "cc": cc,
                      "holder": _COMPANIES[i % len(_COMPANIES)],
                      "validity": "valid", "sig": "typed"})
        i += 1
    for k in range(2):
        specs.append({"template": "voided", "lang": "en", "cc": "US",
                      "holder": _COMPANIES[i % len(_COMPANIES)],
                      "validity": "valid", "sig": "unsigned"})
        i += 1
    # W-9 family
    for spec in (
        {"tin": "EIN", "boxed": False, "cls": "Limited liability company", "signed": True},
        {"tin": "EIN", "boxed": False, "cls": "C Corporation", "signed": False},
        {"tin": "EIN", "boxed": True, "cls": "Limited liability company", "signed": True},
        {"tin": "SSN", "boxed": True, "cls": "Individual/sole proprietor", "signed": True},
        {"tin": "SSN", "boxed": False, "cls": "Individual/sole proprietor", "signed": True},
        {"tin": "EIN", "boxed": False, "cls": "Individual/sole proprietor",
         "signed": True, "conflict": True},
        {"tin": "EIN", "boxed": False, "cls": "Partnership", "signed": True, "dba": True},
        {"tin": "", "boxed": False, "cls": "C Corporation", "signed": True},
    ):
        specs.append({"template": "w9", **spec})
        i += 1
    # --- v3 quality-wave additions (APPENDED: earlier docs stay byte-stable) ---
    specs.append({"template": "letter_role", "lang": "en", "cc": "GB",
                  "holder": _COMPANIES[i % len(_COMPANIES)],
                  "validity": "valid", "sig": "unsigned"}); i += 1
    specs.append({"template": "letter_officer", "lang": "en", "cc": "DE",
                  "holder": _COMPANIES[i % len(_COMPANIES)],
                  "validity": "valid", "sig": "unsigned"}); i += 1
    specs.append({"template": "w9_esig", "tin": "EIN", "boxed": False,
                  "cls": "Limited liability company", "signed": True}); i += 1
    specs.append({"template": "w8bene", "signed": True}); i += 1
    specs.append({"template": "w8bene", "signed": False}); i += 1
    specs.append({"template": "packet3", "lang": "en", "cc": "NL",
                  "holder": _COMPANIES[i % len(_COMPANIES)],
                  "validity": "valid", "sig": "unsigned"}); i += 1
    specs.append({"template": "ssi_letter", "lang": "en", "cc": "FR",
                  "holder": _COMPANIES[i % len(_COMPANIES)],
                  "validity": "valid", "sig": "unsigned"}); i += 1
    specs.append({"template": "zh_notice", "lang": "zh", "cc": "DE",
                  "holder": "假冒贸易有限公司",
                  "validity": "valid", "sig": "unsigned"}); i += 1
    specs.append({"template": "zh_cnaps", "lang": "zh", "cc": "DE",
                  "holder": "假冒服务有限公司",
                  "validity": "valid", "sig": "unsigned"}); i += 1
    # --- v4 TIN-structure wave (APPENDED; W9-040/041) ---
    # Crafted tin_value keeps the PII posture explicit: 36-0000001 has a valid
    # IRS prefix but a synthetic serial; 07-… is a never-assigned prefix
    # (fires W9-040); 99-9999999 is a repeated-digit placeholder (fires W9-041).
    specs.append({"template": "w9", "tin": "EIN", "boxed": False,
                  "cls": "Limited liability company", "signed": True,
                  "tin_value": "36-0000001"}); i += 1
    specs.append({"template": "w9", "tin": "EIN", "boxed": False,
                  "cls": "C Corporation", "signed": True,
                  "tin_value": "07-1234567"}); i += 1
    specs.append({"template": "w9", "tin": "EIN", "boxed": False,
                  "cls": "Partnership", "signed": True,
                  "tin_value": "99-9999999"}); i += 1
    return specs


def _render_pdf(path: Path, text: str) -> None:
    """\f (form feed) in the text forces an explicit page break — multi-page
    templates (W-8, packets) control their page layout with it."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for line in text.splitlines():
        if line == "\f":
            page = doc.new_page()
            y = 80
            continue
        page.insert_text((72, y), line, fontsize=10)
        y += 16
        if y > 780:
            page = doc.new_page()
            y = 80
    doc.set_metadata({})
    doc.save(path)
    doc.close()


def _truth_extraction(doc_class: str, doc_type: str, fields: dict):
    from .fields import Extraction
    ext = Extraction(doc_class=doc_class, doc_type=doc_type)
    ext.fields = dict(fields)
    ext.register_secrets()
    return ext


def _verdict_of(ext) -> tuple[str, list[str]]:
    from .rules.engine import run_rules
    from .verdict import decide
    findings = run_rules(ext, enforce_approvals=False)
    return decide(findings), sorted({f.rule_id for f in findings})


def _det_expected(pdf: Path, doc_class: str) -> dict:
    """What today's DETERMINISTIC pipeline actually produces on the PDF —
    perceive→extract→rules→decide, zero writes outside a temp render dir."""
    from .rules.engine import run_rules
    from .stage_a import perceive
    from .stage_b import extract
    from .verdict import decide
    with tempfile.TemporaryDirectory() as td:
        raw = perceive(pdf, doc_class, Path(td), use_vision=False)
        ext = extract(raw, engine="deterministic")
        findings = run_rules(ext, enforce_approvals=False)
        return {"doc_type": ext.doc_type, "verdict": decide(findings),
                "findings": sorted({f.rule_id for f in findings})}


def _build_doc(spec: dict, idx: int, rng: random.Random, docs_dir: Path) -> dict:
    """-> label row (schema-compatible with dataset/labels.jsonl)."""
    from .privacy import mask
    tpl = spec["template"]
    if tpl == "w9_esig":
        doc_class = "w9"
        holder = _COMPANIES[idx % len(_COMPANIES)]
        person = _PEOPLE[idx % len(_PEOPLE)]
        text, truth = _w9_text(rng, holder, spec["tin"], spec["boxed"],
                               spec["cls"], signed=False)
        text += f"\n{person}\nDigitally signed by {person}\nDate 2026.01.27"
        truth["signed"] = True
        truth["sign_date"] = "2026.01.27"
        doc_type = "w9"
        scen = ["synth_w9", "synth_w9_esig"]
        fname = f"w9_esig_{idx:03d}.pdf"
    elif tpl == "w8bene":
        doc_class = "w9"
        company = "Nordwind Maschinenbau GmbH"
        person = _PEOPLE[idx % len(_PEOPLE)]
        ftin = "DE 29/815/" + "".join(rng.choice("0123456789") for _ in range(5))
        signed = bool(spec.get("signed"))
        pages = [
            "Form W-8BEN-E Certificate of Status of Beneficial Owner for United "
            "States Tax Withholding and Reporting (Entities)\n"
            "Part I  Identification of Beneficial Owner\n"
            f"1 Name of organization that is the beneficial owner: {company}\n"
            "2 Country of incorporation or organization: Germany\n"
            "4 Chapter 3 Status: Corporation\n"
            "5 Chapter 4 Status (FATCA status): Active NFFE. Complete Part XXV.\n"
            "6 Permanent residence address: Musterstrasse 12\n"
            "City or town, state or province, country: 28195 Bremen, Germany\n"
            f"9b Foreign TIN: {ftin}",
            "Part XXV  Active NFFE\n"
            "39 I certify that the entity identified in Part I is a foreign "
            "entity that is an active NFFE.",
            "Part XXX  Certification"
            + (f"\nSign Here  (signature) {person}\nPrint Name of individual "
               f"authorized to sign: {person}\nDate (MM-DD-YYYY): 02-03-2026\n"
               "I certify that I have the capacity to sign for the entity "
               "identified on line 1 of this form." if signed else ""),
        ]
        text = "\n\f\n".join(pages)
        truth = {"form_variant": "W-8BEN-E", "legal_name": company,
                 "country_incorporation": "Germany",
                 "chapter3_status": "Corporation", "chapter4_status": "Active NFFE",
                 "chapter4_cert_section": "Part XXV", "foreign_tin": ftin,
                 "us_tin": "", "address_street": "Musterstrasse 12",
                 "address_city_country": "28195 Bremen, Germany",
                 "treaty_country": "", "signed": signed,
                 "sign_date": "02-03-2026" if signed else "",
                 "signer_name": person if signed else "",
                 "capacity_checked": signed}
        doc_type = "w8"
        scen = ["synth_w8", "synth_w8_signed" if signed else "synth_w8_unsigned"]
        fname = f"w8bene_{idx:03d}.pdf"
    elif tpl == "w9":
        doc_class = "w9"
        holder = _PEOPLE[idx % len(_PEOPLE)] if "Individual" in spec["cls"] \
            else _COMPANIES[idx % len(_COMPANIES)]
        if spec.get("conflict"):
            holder = _COMPANIES[idx % len(_COMPANIES)]     # business name + Individual + EIN
        dba = "Harbor Trading Co" if spec.get("dba") else ""
        text, truth = _w9_text(rng, holder, spec["tin"], spec["boxed"],
                               spec["cls"], spec["signed"], dba,
                               tin_value=spec.get("tin_value", ""))
        if not spec["tin"]:
            truth["tin_raw"] = ""
            truth["tin_type"] = ""
            text = text.replace(f": {truth.get('tin_raw', '')}", ": ")
        doc_type = "w9"
        scen = ["synth_w9", f"synth_tin_{spec['tin'] or 'none'}".lower()]
        if spec["boxed"]:
            scen.append("synth_w9_boxed")
        if spec.get("conflict"):
            scen.append("synth_w9_conflict")
        if spec.get("tin_value"):
            scen.append("synth_w9_tin_crafted")
        fname = f"w9_{idx:03d}.pdf"
    else:
        doc_class = "bank"
        cc = spec.get("cc", "DE")
        holder = spec.get("holder", "")
        rng_iban = synth_iban(cc if cc in _BBAN else "DE", rng)
        iban = break_checksum(rng_iban) if spec["validity"] == "invalid" else rng_iban
        bank, bic = _BANKS.get(cc, _BANKS["DE"])
        person = _PEOPLE[idx % len(_PEOPLE)]
        date = "12 May 2026"
        sig = spec.get("sig", "unsigned")
        signoff = _SIGNOFF[sig].format(tag=f"SYN-{idx:04d}", person=person, bank=bank)
        if tpl in ("letter", "letter_no_holder"):
            lines = list(_LETTER[spec["lang"]])
            if tpl == "letter_no_holder":
                lines[4] = lines[4].replace("{holder}", "the account party on file")
            text = "\n".join(lines).format(bank=bank, holder=holder or "the account party",
                                           iban=iban, bic=bic, date=date, signoff=signoff)
            doc_type = "bank_letter"
        elif tpl == "statement":
            text = "\n".join(_STATEMENT).format(bank=bank, holder=holder or "(name withheld)",
                                                iban=iban)
            if not holder:
                text = text.replace("for account holder (name withheld)", "")
            doc_type = "bank_statement"
        elif tpl == "payment":
            text = "\n".join(_PAYMENT).format(holder=holder, iban=iban, bank=bank)
            doc_type = "payment_instructions"
        elif tpl == "invoice":
            text = "\n".join(_INVOICE).format(num=1000 + idx, date="2026-02-0" + str(1 + idx % 9),
                                              holder=holder, amount="1,200.00", iban=iban)
            doc_type = "invoice"
        elif tpl == "letterhead":
            text = "\n".join(_LETTERHEAD).format(holder=holder, iban=iban, bank=bank,
                                                 person=person)
            doc_type = "supplier_letterhead"
        elif tpl == "voided":
            acct = "".join(rng.choice("0123456789") for _ in range(10))
            text = "\n".join(_VOIDED).format(holder=holder, bank="First Meridian Bank",
                                             acct=acct)
            doc_type = "voided_check"
        elif tpl == "letter_role":
            text = "\n".join([
                bank, "Bank confirmation letter", f"Date: {date}",
                f"This letter is to verify that {holder} is a customer of {bank}.",
                f"IBAN: {iban}", f"BIC: {bic}",
                "Account signatory", person, signoff])
            doc_type = "bank_letter"
        elif tpl == "letter_officer":
            text = "\n".join([
                bank, "Bank confirmation letter", f"Date: {date}",
                "To whom it may concern,",
                f"We confirm that {holder} holds the following account with our bank:",
                f"IBAN: {iban}", f"BIC: {bic}",
                "Sincerely,", person, "Vice President", "Tel: +49 69 555 0000", bank])
            doc_type = "bank_letter"
        elif tpl == "packet3":
            letter = "\n".join(_LETTER["en"]).format(
                bank=bank, holder=holder, iban=iban, bic=bic, date=date,
                signoff=_SIGNOFF["esig"].format(tag=f"SYN-{idx:04d}",
                                                person=person, bank=bank))
            text = "\n\f\n".join([
                f"{holder}\nSupplier banking sheet\nBeneficiary: {holder}\nIBAN {iban}",
                "General terms and conditions.\nNothing of interest on this page.",
                letter])
            doc_type = "bank_letter"
        elif tpl == "ssi_letter":
            text = "\n".join([
                bank, "Standard Settlement Instructions", f"Date: {date}",
                f"Account held with {bank}.", f"Beneficiary: {holder}",
                f"IBAN: {iban}", f"BIC: {bic}",
                "Please use these settlement instructions for all future payments.",
                "Sincerely,", person, "Vice President", "Tel: +33 1 55 00 00 00", bank])
            doc_type = "payment_instructions"
        elif tpl == "zh_cnaps":
            zc_acct = "3531" + "".join(rng.choice("0123456789") for _ in range(13))
            zc_cnaps = "3031" + "".join(rng.choice("0123456789") for _ in range(8))
            text = "\n".join([
                "银行账户信息", f"公司名称: {holder}",
                "开户银行名称: 中国某某银行股份有限公司北京支行",
                f"账号: {zc_acct}", f"联行号: {zc_cnaps}"])
            doc_type = "payment_instructions"
        elif tpl == "zh_notice":
            zh_acct = "6222" + "".join(rng.choice("0123456789") for _ in range(15))
            grouped = " ".join(zh_acct[j:j + 4] for j in range(0, len(zh_acct), 4))
            text = "\n".join([
                "会议通知", "开户银行: 某某银行北京分行",
                f"收款账号: {grouped}", f"户名: {holder}",
                "联系人: 王伟  电话: 13712346060", "会务组"])
            doc_type = "other"
        else:
            raise ValueError(tpl)
        signed_truth = sig in ("esig",)
        evidence = ("DocuSign envelope present" if sig == "esig"
                    else "computer generated confirmation, officer block" if sig == "typed"
                    else "")
        truth = {"account_holder": holder, "bank_name": bank if tpl != "voided" else "First Meridian Bank",
                 "bank_country": cc if cc in _BBAN else "US",
                 "swift_bic": bic if tpl in ("letter", "letter_no_holder") else "",
                 "iban": iban if tpl != "voided" else "",
                 "account_number": acct if tpl == "voided" else "",
                 "routing_aba": "021000021" if tpl == "voided" else "",
                 "signed": signed_truth,
                 "signature_evidence": evidence}
        if tpl == "letter_role":
            truth["account_signatory"] = person
            truth["signature_evidence"] = ""
        elif tpl == "letter_officer":
            truth["signature_evidence"] = "typed officer block"
        elif tpl == "packet3":
            truth["signed"] = True
            truth["signature_evidence"] = "DocuSign envelope present"
        elif tpl == "ssi_letter":
            truth["signature_evidence"] = "typed officer block"
            truth["officer_block"] = True
            truth["settlement_issuer_strong"] = True
        elif tpl == "zh_notice":
            truth.update({"bank_name": "某某银行北京分行", "bank_country": "CN",
                          "swift_bic": "", "iban": "", "account_number": zh_acct})
        elif tpl == "zh_cnaps":
            truth.update({"bank_name": "中国某某银行股份有限公司北京支行",
                          "bank_country": "CN", "swift_bic": "", "iban": "",
                          "account_number": zc_acct,
                          "national_clearing": zc_cnaps,
                          "national_clearing_kind": "CNAPS"})
        scen = [f"synth_{doc_type}", f"synth_lang_{spec.get('lang', 'en')}",
                f"synth_iban_{spec['validity']}", f"synth_sig_{sig}"]
        if tpl in ("letter_role", "packet3"):
            scen.append(f"synth_{tpl}")
        elif tpl == "letter_officer":
            scen.append("synth_officer_block")
        elif tpl == "ssi_letter":
            scen.append("synth_ssi_issuer")
        elif tpl == "zh_notice":
            scen.append("synth_zh")
        elif tpl == "zh_cnaps":
            scen.append("synth_zh_cnaps")
        if not holder and tpl != "invoice":
            scen.append("synth_no_holder")
        fname = f"{doc_type}_{idx:03d}.pdf"

    pdf = docs_dir / fname
    _render_pdf(pdf, text)
    import hashlib
    # content id of the SOURCE TEXT, not the PDF bytes — PDF trailers carry a
    # random /ID so bytes are not stable across saves/PyMuPDF versions. The
    # synthetic stream never joins on run_id (precedents/rescore are real-only).
    sha16 = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    truth_ext = _truth_extraction(doc_class, doc_type, truth)
    verdict_gold, expected_rule_ids = _verdict_of(truth_ext)
    fields_gold = truth_ext.to_public(policy="masked")["fields"]
    det = _det_expected(pdf, doc_class)

    sens = []
    for k in ("iban", "account_number", "routing_aba", "tin_raw", "foreign_tin",
              "national_clearing"):
        v = str(truth.get(k) or "")
        if v:
            kind = ("tin" if k in ("tin_raw", "foreign_tin")
                    else "iban" if k == "iban" else "account_number")
            sens.append({"kind": kind, "masked": mask(kind, v), "fake": v})

    return {"label_id": f"synth-{GENERATOR_VERSION}-{idx:03d}",
            "ts": "2026-07-09T00:00:00Z",                  # fixed: determinism
            "doc_sha256": sha16,
            "doc_path": fname,
            "doc_class": doc_class,
            "doc_type_gold": doc_type,
            "fields_gold": fields_gold,
            "verdict_gold": verdict_gold,
            "expected_rule_ids": expected_rule_ids,
            "det_expected": det,
            "sensitive_map": sens,
            "reviewer": "synth",
            "notes": f"synthetic template={spec['template']}",
            "scenarios": scen,
            "source": "synthetic",
            "generator": {"seed": rng_seed_of(rng), "version": GENERATOR_VERSION,
                          "template_id": spec["template"]},
            "confirmed": False}


_SEED_BOX: dict = {}


def rng_seed_of(rng: random.Random) -> int:
    return _SEED_BOX.get(id(rng), DEFAULT_SEED)


def generate(seed: int = DEFAULT_SEED, out_dir: Path | None = None) -> dict:
    """Generate the corpus. Returns {count, agreement, labels_path}."""
    from .privacy import assert_no_leak
    out = out_dir or config.SYNTH_DIR
    docs_dir = out / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    _SEED_BOX[id(rng)] = seed
    specs = _spec_list(rng)
    rows = []
    agree = 0
    for idx, spec in enumerate(specs):
        row = _build_doc(spec, idx, rng, docs_dir)
        blob = json.dumps(row, ensure_ascii=False)
        # no REAL secrets exist by construction; the invented identities are
        # registered as fakes so the strict pattern sweep allows them
        assert_no_leak(blob, [], allowed_fakes=[it["fake"] for it in row["sensitive_map"]])
        agree += int(row["det_expected"]["verdict"] == row["verdict_gold"])
        rows.append(row)
    (out / "labels.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    matrix: dict = {}
    for r in rows:
        for s in r["scenarios"]:
            matrix[s] = matrix.get(s, 0) + 1
    (out / "MANIFEST.json").write_text(json.dumps(
        {"seed": seed, "generator_version": GENERATOR_VERSION, "count": len(rows),
         "truth_vs_deterministic_agreement": round(agree / len(rows), 3),
         "matrix": dict(sorted(matrix.items()))}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    return {"count": len(rows), "agreement": round(agree / len(rows), 3),
            "labels_path": out / "labels.jsonl"}


def check(seed: int = DEFAULT_SEED) -> int:
    """Staleness/determinism self-check: regenerate into a temp dir and compare
    labels bytes + per-doc extracted text against the committed corpus."""
    committed = config.SYNTH_DIR / "labels.jsonl"
    if not committed.exists():
        print("no committed synthetic corpus — run `mdmdoc synth-gen` first")
        return 1
    with tempfile.TemporaryDirectory() as td:
        generate(seed=seed, out_dir=Path(td))
        fresh = (Path(td) / "labels.jsonl").read_text(encoding="utf-8")
        if fresh != committed.read_text(encoding="utf-8"):
            print("STALE: committed labels differ from a fresh regeneration "
                  "(rules/templates/generator changed) — re-run `mdmdoc synth-gen` "
                  "and review the labels diff")
            return 1
        import fitz
        for lab in (json.loads(l) for l in fresh.splitlines() if l.strip()):
            a = config.SYNTH_DIR / "docs" / lab["doc_path"]
            b = Path(td) / "docs" / lab["doc_path"]
            ta = "\n".join(p.get_text() for p in fitz.open(a))
            tb = "\n".join(p.get_text() for p in fitz.open(b))
            if ta != tb:
                print(f"STALE: {lab['doc_path']} text differs from regeneration")
                return 1
    print("synthetic corpus is fresh (labels + per-doc text match regeneration)")
    return 0
