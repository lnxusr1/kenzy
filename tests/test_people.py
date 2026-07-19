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


# -- write path (dashboard People panel) ------------------------------------


def test_save_new_person_generates_slug_id_and_persists(tmp_path):
    s = PeopleStore(tmp_path / "people.yaml")  # start empty
    p = s.save_person(id=None, name="Alice Smith", voiceprints=["alice", " ", "alice"])
    assert p.id == "alice_smith"  # slug from the name
    assert p.voiceprints == ["alice"]  # trimmed + de-duped, blanks dropped
    # Written to disk and readable by a fresh store (the pipeline's boot path).
    fresh = PeopleStore(tmp_path / "people.yaml")
    assert fresh.by_voiceprint("alice").id == "alice_smith"


def test_save_updates_existing_and_reindexes_live(tmp_path):
    s = _store(tmp_path)
    s.save_person(id="john", name="John", voiceprints=["john"])  # drop 'johnmark'
    assert s.by_voiceprint("johnmark") is None  # index updated in-process
    assert s.by_voiceprint("john").id == "john"


def test_save_moves_voiceprint_off_prior_owner(tmp_path):
    s = _store(tmp_path)
    # Assign john's 'johnmark' voice to nicki — it must leave john.
    s.save_person(id="nicki", name="Nicki", voiceprints=["nicki", "johnmark"])
    assert s.by_voiceprint("johnmark").id == "nicki"
    assert s.get("john").voiceprints == ["john"]


def test_save_preserves_reserved_ha_user_link(tmp_path):
    s = _store(tmp_path)
    s.save_person(id="john", name="Johnny", voiceprints=["john", "johnmark"])
    assert s.get("john").ha_user == "person.john"  # UI never sends it, never clobbers it
    fresh = PeopleStore(tmp_path / "people.yaml")
    assert fresh.get("john").ha_user == "person.john"  # round-trips through the file


def test_save_slug_id_dedupes_on_name_collision(tmp_path):
    s = PeopleStore(tmp_path / "people.yaml")
    a = s.save_person(id=None, name="Sam", voiceprints=[])
    b = s.save_person(id=None, name="Sam", voiceprints=[])
    assert (a.id, b.id) == ("sam", "sam_2")


def test_save_blank_name_rejected(tmp_path):
    s = PeopleStore(tmp_path / "people.yaml")
    try:
        s.save_person(id=None, name="   ", voiceprints=["x"])
    except ValueError:
        pass
    else:
        raise AssertionError("blank name should raise")


def test_by_name_matches_display_or_id(tmp_path):
    s = _store(tmp_path)
    assert s.by_name("john").id == "john"  # by id
    assert s.by_name(" NICKI ").id == "nicki"  # by display name, case/space-insensitive
    assert s.by_name("stranger") is None


def test_slugify():
    from kenzy.server.people import slugify

    assert slugify("Alice Smith") == "alice_smith"
    assert slugify("Uncle Bob!") == "uncle_bob"
    assert slugify("---") == "person"  # never empty


def test_rename_voiceprint_follows_owner(tmp_path):
    s = _store(tmp_path)
    assert s.rename_voiceprint("johnmark", "jm") is True
    assert s.by_voiceprint("jm").id == "john"
    assert s.by_voiceprint("johnmark") is None
    fresh = PeopleStore(tmp_path / "people.yaml")
    assert fresh.get("john").voiceprints == ["john", "jm"]  # persisted
    assert s.rename_voiceprint("stranger", "x") is False  # unowned voice: no-op


def test_remove_voiceprint_drops_from_owner(tmp_path):
    s = _store(tmp_path)
    assert s.remove_voiceprint("JOHNMARK") is True  # case-insensitive
    assert s.get("john").voiceprints == ["john"]  # person stays, voice gone
    assert s.by_voiceprint("johnmark") is None
    assert s.remove_voiceprint("stranger") is False


def test_delete_person(tmp_path):
    s = _store(tmp_path)
    assert s.delete_person("john") is True
    assert s.get("john") is None
    assert s.by_voiceprint("johnmark") is None  # index dropped
    assert s.delete_person("john") is False  # already gone
    fresh = PeopleStore(tmp_path / "people.yaml")
    assert {p.id for p in fresh.all()} == {"nicki"}  # persisted


# -- F3: HA Assist identity (the second front door) ---------------------------


def test_by_ha_user_and_assist_resolution(tmp_path):
    from kenzy.server.people import resolve_assist_identity

    s = _store(
        tmp_path,
        "people:\n"
        "  john:\n    name: John\n    voiceprints: [johnmark]\n    ha_user: person.john_mark\n"
        "  nicki:\n    name: Nicki\n    voiceprints: [nicki]\n    ha_user: person.nicki\n",
    )
    assert s.by_ha_user("person.john_mark").id == "john"
    assert s.by_ha_user("PERSON.NICKI").id == "nicki"  # case-insensitive
    assert s.by_ha_user("person.stranger") is None
    assert s.by_ha_user("") is None

    ident = resolve_assist_identity(s, "person.john_mark", unknown_name="unknown")
    assert ident.display == "John" and ident.person_id == "john"
    assert ident.tier == TIER_RECOGNIZED and ident.recognized
    # Unmapped HA user ⇒ unknown, fail closed (no memory, gated skills withheld).
    ident = resolve_assist_identity(s, "person.guest", unknown_name="unknown")
    assert ident.tier == TIER_UNKNOWN and not ident.recognized and ident.person_id is None


def test_save_person_ha_user_three_state(tmp_path):
    s = PeopleStore(tmp_path / "people.yaml")
    p = s.save_person(id=None, name="John", voiceprints=[], ha_user="person.john_mark")
    assert p.ha_user == "person.john_mark"
    # Omitted ⇒ preserved (the dashboard's name/voice-only saves).
    s.save_person(id=p.id, name="Johnny", voiceprints=[])
    assert s.get(p.id).ha_user == "person.john_mark"
    # Explicit empty ⇒ cleared.
    s.save_person(id=p.id, name="Johnny", voiceprints=[], ha_user="")
    assert s.get(p.id).ha_user is None
    # Round-trips through the file.
    s.save_person(id=p.id, name="Johnny", voiceprints=[], ha_user="person.j")
    assert PeopleStore(tmp_path / "people.yaml").by_ha_user("person.j") is not None


def test_audioserver_save_person_ha_user_passthrough(tmp_path, monkeypatch):
    # The dashboard mutation threads ha_user through AudioServer.save_person
    # with the same three-state semantics as the store.
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    from kenzy.server.server import AudioServer

    s = AudioServer({})
    pid = s.save_person("", "John", [], ha_user="person.john_mark")
    assert s._people.get(pid).ha_user == "person.john_mark"
    # Omitted ⇒ preserved.
    s.save_person(pid, "John", [])
    assert s._people.get(pid).ha_user == "person.john_mark"
    # Explicit empty ⇒ cleared.
    s.save_person(pid, "John", [], ha_user="")
    assert s._people.get(pid).ha_user is None


def test_memory_capture_setting_roundtrip(tmp_path, monkeypatch):
    # 4.1 capture modes: default explicit, validated set, preserved on omit.
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    from kenzy.server.server import AudioServer

    s = AudioServer({})
    pid = s.save_person("", "John", [])
    assert s._people.get(pid).memory_capture == "explicit"
    s.save_person(pid, "John", [], memory_capture="auto")
    assert s._people.get(pid).memory_capture == "auto"
    s.save_person(pid, "John", [])  # omitted ⇒ preserved
    assert s._people.get(pid).memory_capture == "auto"
    s.save_person(pid, "John", [], memory_capture="bogus")  # invalid ⇒ preserved
    assert s._people.get(pid).memory_capture == "auto"
    # Survives reload + threads into the /process payload helper.
    s2 = AudioServer({})
    assert s2._people.get(pid).memory_capture == "auto"
    from kenzy.server.people import Identity

    ident = Identity(display="John", tier="recognized", confidence=1.0, person_id=pid)
    assert s2._person_memory_capture(ident) == "auto"
    assert s2._person_memory_capture(None) == "explicit"
