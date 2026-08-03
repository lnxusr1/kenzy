"""Spoken-name resolution (5.0.3 slice D): the transcriber spells names its own
way per speaker, so the resolver must absorb spelling drift without ever
guessing between two household members or leaking into asker identity.

Every variant class named in the design gets a case here, and so do both traps:
a near-tie returns *ambiguous* rather than a winner, and garbage/empty input
fails closed.
"""

from __future__ import annotations

from kenzy.llm.names import resolve_person

BOBBIE = {"id": "bobbie", "name": "Bobbie", "ha_user": "person.bobbie"}
VICKI = {"id": "vicki", "name": "Vicki", "ha_user": "person.vicki"}
SARAH = {"id": "sarah", "name": "Sarah", "ha_user": "person.sarah"}
ROBERT = {"id": "robert", "name": "Robert", "aliases": ["Bud"], "ha_user": "person.robert"}
PEOPLE = [BOBBIE, VICKI, SARAH, ROBERT]


# ---------------------------------------------------------------------------
# Exact and alias
# ---------------------------------------------------------------------------


def test_exact_name_and_id_still_win():
    assert resolve_person("Bobbie", PEOPLE).person["id"] == "bobbie"
    assert resolve_person("vicki", PEOPLE).person["id"] == "vicki"
    assert resolve_person("  Sarah. ", PEOPLE).person["id"] == "sarah"  # punctuation


def test_alias_resolves_what_no_metric_could():
    r = resolve_person("Bud", PEOPLE)
    assert r.person["id"] == "robert" and r.via == "alias"


def test_exact_beats_fuzzy_outright():
    # "Jon" exact + "John" fuzzy-close: saying precisely what a record is
    # called must win uniquely, never read as a tie with a near-miss.
    people = [{"id": "jon", "name": "Jon"}, {"id": "john", "name": "John"}]
    r = resolve_person("Jon", people)
    assert r.person["id"] == "jon" and not r.is_ambiguous


# ---------------------------------------------------------------------------
# The variant classes STT actually produces
# ---------------------------------------------------------------------------


def test_ie_vs_y_ending():
    r = resolve_person("Bobby", PEOPLE)  # the …ie/…y drift class
    assert r.person["id"] == "bobbie" and r.via == "fuzzy"


def test_doubled_consonant():
    assert resolve_person("Vikki", PEOPLE).person["id"] == "vicki"


def test_trailing_h():
    assert resolve_person("Sara", PEOPLE).person["id"] == "sarah"


# ---------------------------------------------------------------------------
# The traps
# ---------------------------------------------------------------------------


def test_near_tie_is_ambiguous_never_a_guess():
    people = [{"id": "jon", "name": "Jon"}, {"id": "john", "name": "John"}]
    r = resolve_person("Jhon", people)  # matches neither exactly, both closely
    assert r.is_ambiguous
    assert {c["id"] for c in r.candidates} == {"jon", "john"}


def test_duplicate_exact_labels_surface_as_ambiguous():
    # Two records answering to the same string is a data problem the resolver
    # must surface as a question, never settle by iteration order.
    people = [
        {"id": "j1", "name": "Jay"},
        {"id": "j2", "name": "Junior", "aliases": ["Jay"]},
    ]
    assert resolve_person("Jay", people).is_ambiguous


def test_non_names_and_garbage_fail_closed():
    assert resolve_person("my phone", PEOPLE).is_none
    assert resolve_person("the office lamps", PEOPLE).is_none
    assert resolve_person("", PEOPLE).is_none
    assert resolve_person("   ?!  ", PEOPLE).is_none
    assert resolve_person("Bobby", []).is_none


def test_resolver_output_is_a_copy():
    # Whatever a skill does to the returned dict must not mutate the injected
    # request context that later skills read.
    r = resolve_person("Bobbie", PEOPLE)
    r.person["name"] = "mangled"
    assert BOBBIE["name"] == "Bobbie"
