"""v4 F1 identity core: person store + voice resolver."""

from __future__ import annotations

from kenzy.server.people import (
    TIER_RECOGNIZED,
    TIER_UNKNOWN,
    PeopleStore,
    resolve_voice_identity,
)

_YAML = """\
people:
  john:
    name: John
    voiceprints: [john, johnmark]
    ha_user: person.john
  nicki:
    name: Nicki
    voiceprints: [nicki]
"""


def _store(tmp_path, text=_YAML):
    p = tmp_path / "people.yaml"
    p.write_text(text)
    return PeopleStore(p)


def test_store_indexes_by_voiceprint(tmp_path):
    s = _store(tmp_path)
    assert s.by_voiceprint("john").id == "john"
    assert s.by_voiceprint("johnmark").id == "john"  # multiple voiceprints -> one person
    assert s.by_voiceprint("JOHN").id == "john"  # case-insensitive
    assert s.by_voiceprint("stranger") is None
    assert {p.id for p in s.all()} == {"john", "nicki"}


def test_absent_file_is_empty_store_passthrough(tmp_path):
    s = PeopleStore(tmp_path / "nope.yaml")  # no file
    assert s.all() == []
    ident = resolve_voice_identity(s, "john", 0.7, unknown_name="unknown")
    assert ident.display == "john"  # passthrough — exactly today's behavior
    assert ident.tier == TIER_RECOGNIZED
    assert ident.person_id is None


def test_resolve_known_person(tmp_path):
    s = _store(tmp_path)
    ident = resolve_voice_identity(s, "johnmark", 0.82, unknown_name="unknown")
    assert ident.display == "John"  # person's display name, not the voiceprint
    assert ident.person_id == "john"
    assert ident.tier == TIER_RECOGNIZED
    assert ident.ha_user == "person.john"
    assert ident.recognized


def test_resolve_unknown(tmp_path):
    s = _store(tmp_path)
    ident = resolve_voice_identity(s, "unknown", 0.1, unknown_name="unknown")
    assert ident.tier == TIER_UNKNOWN
    assert ident.display == "unknown"
    assert not ident.recognized


def test_recognized_voice_without_record_passes_through(tmp_path):
    s = _store(tmp_path)
    ident = resolve_voice_identity(s, "bob", 0.6, unknown_name="unknown")
    assert ident.tier == TIER_RECOGNIZED  # a real voiceprint, just no person record yet
    assert ident.display == "bob"
    assert ident.person_id is None


def test_malformed_file_degrades_to_empty(tmp_path):
    s = _store(tmp_path, "not: [valid: people")
    assert s.all() == []  # never crash the pipeline on a bad file


def test_ha_optional(tmp_path):
    # A voiceprint-only person (no ha_user) resolves fine — standalone-without-HA.
    s = _store(tmp_path, "people:\n  guest:\n    name: Guest\n    voiceprints: [guest]\n")
    ident = resolve_voice_identity(s, "guest", 0.7, unknown_name="unknown")
    assert ident.person_id == "guest" and ident.ha_user is None
