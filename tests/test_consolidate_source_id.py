"""source_id — generation, validation, collisions."""
from __future__ import annotations

from mdmdoc.consolidation import source_id as sidmod


def test_generated_shape_and_determinism():
    assert sidmod.generate(set(), today="20260710") == "NEW_20260710_01"
    assert sidmod.generate(set(), today="20260710") == "NEW_20260710_01"


def test_collision_gets_next_ordinal():
    existing = {"NEW_20260710_01", "NEW_20260710_02"}
    assert sidmod.generate(existing, today="20260710") == "NEW_20260710_03"


def test_problems_empty_for_good_id():
    assert sidmod.problems("NEW_20260710_01", set()) == []
    assert sidmod.problems("38300", set()) == []


def test_problems_charset_and_length():
    assert sidmod.problems("has space", set())
    assert sidmod.problems("x" * 21, set())
    assert sidmod.problems("", set())


def test_problems_collision_names_the_merge_risk():
    msgs = sidmod.problems("OLD_1", {"OLD_1"})
    assert msgs and "merge" in msgs[0]
