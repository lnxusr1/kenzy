"""Music Assistant play-by-name passthrough (4.3): registry-based player
tagging (rename-proof), the fast intent's guard rails, room targeting, and
the LLM-tier tool's honest replies."""

from __future__ import annotations

import pytest

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.builtin_skills import home_assistant as ha


def _model():
    E = ha_model.Entity
    ents = [
        # MA players (registry-tagged) — names deliberately unhelpful: the
        # tagging must work no matter what the operator renamed things to.
        E("media_player.office_x", "media_player", "Office Music Player", "office", "Office",
          "main", "Main", ma=True),
        E("media_player.loft_y", "media_player", "Loft Music Player", "loft", "Loft",
          "main", "Main", ma=True),
        # The same rooms' TVs — plain cast/Roku players, NOT music targets.
        E("media_player.office_tv", "media_player", "Office TV", "office", "Office",
          "main", "Main"),
        E("media_player.loft_tv", "media_player", "Loft TV", "loft", "Loft", "main", "Main"),
        # A light for bucket noise.
        E("light.office_lamp", "light", "Office Lamp", "office", "Office", "main", "Main"),
    ]
    return ha_model.HAModel(entities=ents, fetched_at=1.0)


@pytest.fixture()
def idx(monkeypatch):
    index = ha._index_from_model(_model(), {})

    async def ensure():
        return None

    monkeypatch.setattr(ha, "_ensure_view", ensure)
    monkeypatch.setattr(ha, "_get_index", lambda: index)
    return index


@pytest.fixture()
def played(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_play(entity_id, query, *, radio=False):
        calls.append((entity_id, query))

    monkeypatch.setattr(ha, "_ma_play", fake_play)
    return calls


def test_index_separates_music_from_tvs(idx):
    assert idx.rooms["office"]["music"] == ["media_player.office_x"]
    # TVs stay in the media bucket (transport verbs), alongside the MA player.
    assert set(idx.rooms["office"]["media"]) == {
        "media_player.office_x", "media_player.office_tv"
    }


async def test_play_targets_asking_room(idx, played):
    r = await ha.fast_play("Play some jazz.", "Office", None)
    assert r.is_handled and "jazz" in r.text.lower()
    assert played == [("media_player.office_x", "jazz")]  # "some " stripped


async def test_play_named_room_and_title_case_kept(idx, played):
    r = await ha.fast_play("Play The Beatles in the loft.", "Office", None)
    assert r.is_handled
    assert played == [("media_player.loft_y", "The Beatles")]


async def test_title_containing_in_the_stays_whole(idx, played):
    await ha.fast_play("Play Dancing in the Dark.", "Office", None)
    assert played == [("media_player.office_x", "Dancing in the Dark")]


async def test_room_without_player_answers_honestly(idx, played, monkeypatch):
    # Make a room known to the index but music-less.
    idx.room_phrases["garage"] = "garage"
    r = await ha.fast_play("Play blues in the garage.", "Office", None)
    assert r.is_handled and "no music player" in r.text.lower()
    assert played == []


async def test_guards_miss_to_llm(idx, played):
    for utt in ("Play.", "Play it.", "Play some music.", "Play the next song.",
                "Playing with fire", "Pause."):
        r = await ha.fast_play(utt, "Office", None)
        assert r.status == "miss", utt
    assert played == []


async def test_no_ma_house_always_misses(monkeypatch, played):
    ents = [e for e in _model().entities if not e.ma]
    index = ha._index_from_model(ha_model.HAModel(entities=ents, fetched_at=1.0), {})

    async def ensure():
        return None

    monkeypatch.setattr(ha, "_ensure_view", ensure)
    monkeypatch.setattr(ha, "_get_index", lambda: index)
    r = await ha.fast_play("Play some jazz.", "Office", None)
    assert r.status == "miss" and played == []


async def test_play_failure_misses_to_llm(idx, monkeypatch):
    async def boom(entity_id, query, *, radio=False):
        raise RuntimeError("MA down")

    monkeypatch.setattr(ha, "_ma_play", boom)
    r = await ha.fast_play("Play some jazz.", "Office", None)
    assert r.status == "miss"


async def test_llm_tool_paths(idx, played, monkeypatch):
    tok = sk._request_ctx.set({"room_id": "Office"})
    try:
        out = await ha.play_music("miles davis")
        assert "Playing miles davis" in out
        assert played[-1] == ("media_player.office_x", "miles davis")

        out = await ha.play_music("blues", room="garage")
        assert "no music player" in out.lower()

        # Ambiguous (no origin, two rooms have players) → asks which room.
        sk._request_ctx.reset(tok)
        tok = sk._request_ctx.set({"room_id": None})
        out = await ha.play_music("jazz")
        assert "Which room" in out and "loft" in out and "office" in out
    finally:
        sk._request_ctx.reset(tok)


def test_control_verbs_never_see_ma_players_even_as_name_twins():
    """The 4.3.0 field bug: MA imports arrive named after the device they wrap
    — TWO "Office TV" entries — and "turn on the TV" actuated the MA queue
    frontend. Control resolution must be blind to MA players regardless of
    naming; only music + transport may target them."""
    E = ha_model.Entity
    ents = [
        E("media_player.office_tv", "media_player", "Office TV", "office", "Office",
          "main", "Main"),
        # The nightmare twin: an MA player with the IDENTICAL spoken name.
        E("media_player.office_ma", "media_player", "Office TV", "office", "Office",
          "main", "Main", ma=True),
    ]
    idx = ha._index_from_model(ha_model.HAModel(entities=ents, fetched_at=1.0), {})

    assert idx.music_only == {"media_player.office_ma"}
    # Exact-name resolution: only the real TV, never the twin.
    assert ha._exact_named(idx, "office", "office tv") == ["media_player.office_tv"]
    # Room-word-stripped singular ("the tv"): same.
    assert ha._room_named_device(idx, "office", "tv") == ["media_player.office_tv"]
    # Fuzzy (room and house-wide): same.
    assert ha._fuzzy(idx, "office", "office tv") == "media_player.office_tv"
    assert ha._fuzzy(idx, None, "office tv") == "media_player.office_tv"
    # But transport still sees both (media bucket) and music sees the MA one.
    assert set(idx.rooms["office"]["media"]) == {
        "media_player.office_tv", "media_player.office_ma"
    }
    assert idx.rooms["office"]["music"] == ["media_player.office_ma"]
