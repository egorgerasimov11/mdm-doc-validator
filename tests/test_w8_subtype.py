"""W1: the W-8 subtype — own schema/prompt/report inside doc_class w9, the
foreign-TIN protection, the exemplar-echo hardening. Root: real W-8BEN-E run
that was pressed through the W-9 schema (line1=ACME echo, Foreign TIN offered
for Tax Number 2)."""
import json

import pytest

from mdmdoc import config, report, stage_b
from mdmdoc.evalrun import W8_SCORED, W9_SCORED, _scored_keys
from mdmdoc.fields import W8_KEYS, Extraction, crosscheck_ids
from mdmdoc.stage_a import RawDoc

W8_TEXT = ("Form W-8BEN-E Certificate of Status of Beneficial Owner. "
           "Part I Identification of Beneficial Owner. "
           "Part XXX Certification.")


def _raw(doc_class="w9", type_hint="w8", text=W8_TEXT):
    r = RawDoc(path="/x/form.pdf", sha256="d" * 16, ext=".pdf", doc_class=doc_class)
    r.raw_text = text
    r.type_hint = type_hint
    return r


def test_is_w8_gate():
    assert stage_b._is_w8(_raw()) is True
    assert stage_b._is_w8(_raw(type_hint="w9")) is False
    assert stage_b._is_w8(_raw(doc_class="bank")) is False


def test_build_prompt_uses_w8_schema_and_injects_fewshot(monkeypatch):
    monkeypatch.setattr(stage_b.mc, "resolve", lambda role: "mdmdoc-extract")
    p = stage_b.build_prompt(_raw())
    assert "legal_name" in p and "chapter4_cert_section" in p
    assert "tin_raw" not in p                     # W-9 schema keys are gone
    assert "EXAMPLE INPUT" in p                   # injected even for the baked model
    assert "Nordwind Maschinenbau GmbH" in p      # the invented w8 exemplar


def test_extract_forces_doc_type_w8_deterministically():
    ext = stage_b.extract(_raw(), engine="deterministic")
    assert ext.doc_type == "w8"
    assert ext.provenance["doc_type"]["source"] == "rule"
    assert set(W8_KEYS) <= set(ext.fields)


def test_ein_candidate_never_fills_w8_fields():
    """The GVS failure: an EIN-shaped FOREIGN TIN must not become tin_raw."""
    fields = {k: "" for k in W8_KEYS}
    notes = crosscheck_ids(fields, {"ein": "15-5320496"}, "w9")
    assert "tin_raw" not in fields
    assert not any("filled-from-OCR" in n for n in notes)


def test_foreign_tin_follows_the_tin_policy(monkeypatch):
    """The W-8 tax numbers share the W-9 TIN contract exactly: revealed on the
    operator console, masked whenever the caller or the policy says so."""
    ext = Extraction(doc_class="w9", doc_type="w8")
    ext.fields = {k: "" for k in W8_KEYS}
    ext.fields["foreign_tin"] = "DE 29/815/44444"

    ftin = ext.to_public(policy="full")["fields"]["foreign_tin"]
    assert ftin["present"] is True and ftin["value"] == "DE 29/815/44444"
    assert "44444" not in ftin["masked"]

    assert "value" not in ext.to_public(policy="masked")["fields"]["foreign_tin"]
    monkeypatch.setenv("MDMDOC_TIN_VALUES", "masked")
    ftin = ext.to_public(policy="full")["fields"]["foreign_tin"]
    assert ftin["present"] is True and "value" not in ftin
    assert "44444" not in ftin["masked"]


def test_w8_escalation_reasons():
    ext = Extraction(doc_class="w9", doc_type="w8")
    ext.fields = {k: "" for k in W8_KEYS}
    ext.json_valid_first_try = True
    r = stage_b.escalation_reasons(ext, _raw(), quality=False)
    assert "w8-no-name" in r and "w8-no-country" in r
    assert "w9-no-tin" not in r
    assert "w8-no-name" in stage_b.FOCUS_HINTS and "w8-no-country" in stage_b.FOCUS_HINTS


def _pub(fields, doc_type="w8"):
    return {"doc_class": "w9", "doc_type": doc_type, "file_name": "f.pdf",
            "fields": fields, "crosscheck": [], "warnings": []}


def test_w8_report_template_and_tin_mapping_warning():
    ext = Extraction(doc_class="w9", doc_type="w8")
    ext.fields = {k: "" for k in W8_KEYS}
    ext.fields.update({"form_variant": "W-8BEN-E", "legal_name": "Nord Fake GmbH",
                       "country_incorporation": "Germany",
                       "foreign_tin": "DE 29/815/44444", "signed": True})
    pub = ext.to_public()
    pub["file_name"] = "f.pdf"
    md = report.render_report(pub, [], "NEED_MANUAL_REVIEW")
    assert "W-8 CHECK" in md
    assert "NEVER enter into SAP Tax Number 1 or Tax Number 2" in md
    assert "44444" not in md                       # masked everywhere


def test_model_guessed_w8_over_w9_schema_falls_back_to_w9_report():
    md = report.render_report(_pub({"line1_name": "Someone", "signed": False}), [],
                              "NEED_MANUAL_REVIEW")
    assert "W-9 CHECK" in md and "W-8 CHECK" not in md


def test_baked_snapshot_and_placeholder_denylist(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path)
    (tmp_path / "exemplar_values.json").write_text(
        json.dumps(["Baked Fake Industries"]))
    raw = _raw(type_hint="w9", text="an unrelated document text")
    ext = Extraction(doc_class="w9", doc_type="w9")
    ext.fields = {"line1_name": "Baked Fake Industries",
                  "line2_business_name": "ACME"}
    stage_b._drop_exemplar_echo(ext, raw)
    assert ext.fields["line1_name"] == ""          # baked-snapshot echo dropped
    assert ext.fields["line2_business_name"] == ""  # placeholder denylist
    assert len([w for w in ext.warnings if "dropped" in w]) == 2


def test_placeholder_kept_when_printed_in_document():
    raw = _raw(type_hint="w9", text="Supplier: ACME LLC, Chicago")
    ext = Extraction(doc_class="w9", doc_type="w9")
    ext.fields = {"line1_name": "ACME"}
    stage_b._drop_exemplar_echo(ext, raw)
    assert ext.fields["line1_name"] == "ACME"      # literally printed -> legit


def test_eval_scored_keys_for_w8_gold():
    assert _scored_keys({"doc_class": "w9", "doc_type_gold": "w8"}) == W8_SCORED
    assert _scored_keys({"doc_class": "w9", "doc_type_gold": "w9"}) == W9_SCORED
