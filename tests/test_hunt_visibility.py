"""F1: a targeted-hunt reply must be VISIBLE to the extraction model (it goes
into raw_text, surviving Stage B's text cut) while type signals stay frozen on
the pre-hunt text (a hunt can never flip the classification between runs)."""
from mdmdoc import config
from mdmdoc.stage_a import RawDoc, _append_hunt_text


def _raw(text: str) -> RawDoc:
    r = RawDoc(path="/x/d.pdf", sha256="a" * 64, ext=".pdf", doc_class="bank")
    r.raw_text = text
    return r


def test_hunt_block_lands_in_raw_text():
    r = _raw("Bank letter body")
    _append_hunt_text(r, "holder search", "Account holder: Acme GmbH\nBank: Commerzbank")
    assert "[holder search]" in r.raw_text
    assert "Account holder: Acme GmbH" in r.raw_text
    assert r.raw_text.startswith("Bank letter body")


def test_hunt_block_survives_stage_b_cut():
    # base text alone overflows the Stage B budget — the BASE gets trimmed,
    # the hunt block keeps the tail (otherwise the model never sees it)
    base = "x" * (config.STAGE_B_TEXT_LIMIT + 500)
    r = _raw(base)
    _append_hunt_text(r, "holder search", "Account holder: Acme GmbH")
    assert len(r.raw_text) <= config.STAGE_B_TEXT_LIMIT
    assert r.raw_text.endswith("Account holder: Acme GmbH")
    assert "[holder search]" in r.raw_text[: config.STAGE_B_TEXT_LIMIT]


def test_type_hint_is_frozen_before_hunts():
    # perceive() computes type_hint from the PRE-hunt snapshot; appending hunt
    # text with classification-bearing words must not touch it
    r = _raw("Wire instructions for the beneficiary")
    r.type_hint = "other"
    _append_hunt_text(r, "targeted search", "invoice no. 4711 total due 99,00")
    assert r.type_hint == "other"
