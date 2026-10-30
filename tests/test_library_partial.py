"""Partial library albums (fewer songs than the album has), for the apps' Albums."""
from unittest.mock import AsyncMock, patch

from conftest import bearer, login, make_user, run

from continuo.db import library as lib_db
from continuo.db import track_counts
from continuo.library import completeness

ALBUMS = [
    {"navidrome_id": "nd-half", "name": "Half", "artist": "A", "song_count": 4, "folder": "A/Half", "mbz_release_group_id": "rg-half"},
    {"navidrome_id": "nd-full", "name": "Full", "artist": "A", "song_count": 10, "folder": "A/Full", "mbz_release_group_id": "rg-full"},
    {"navidrome_id": "nd-new", "name": "New", "artist": "A", "song_count": 3, "folder": "A/New", "mbz_release_group_id": "rg-new"},
    {"navidrome_id": "nd-one", "name": "One", "artist": "A", "song_count": 1, "folder": "_singles/A/One", "mbz_release_group_id": "rg-one"},
]


def test_partial_albums_are_listed_complete_and_uncounted_ones_not(client):
    make_user()
    s = login(client)
    lib_db.upsert_many(ALBUMS)
    track_counts.put("rg-half", 12)
    track_counts.put("rg-full", 10)
    track_counts.put("rg-one", 9)         # a single song isn't a partial album: it's under Songs
    r = client.get("/v1/library/partial", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json() == {"albums": [{"navidromeAlbumId": "nd-half", "have": 4, "expected": 12}]}


def test_the_background_count_fills_what_isnt_counted_and_skips_singles(client):
    lib_db.upsert_many(ALBUMS)
    track_counts.put("rg-full", 10)
    assert completeness.uncounted(lib_db.all_albums(), track_counts.all_counts()) == ["rg-half", "rg-new"]
    fake = AsyncMock(side_effect=lambda rg, strict=False: [{"title": str(i)} for i in range(12 if rg == "rg-half" else 3)])
    with patch("continuo.upstream.musicbrainz.album_tracks", fake):
        run(completeness.tick())
    assert track_counts.all_counts() == {"rg-full": 10, "rg-half": 12, "rg-new": 3}
    assert completeness.uncounted(lib_db.all_albums(), track_counts.all_counts()) == []


def test_musicbrainz_down_leaves_it_for_the_next_tick(client):
    from continuo.upstream import musicbrainz
    lib_db.upsert_many(ALBUMS)
    with patch("continuo.upstream.musicbrainz.album_tracks", AsyncMock(side_effect=musicbrainz.Unavailable("busy"))):
        run(completeness.tick())
    assert track_counts.all_counts() == {}


def test_untagged_albums_are_matched_by_name_then_counted(client):
    from continuo.db import identity
    lib_db.upsert_many([
        {"navidrome_id": "nd-u", "name": "Geogaddi", "artist": "Boards of Canada", "song_count": 20, "folder": "BoC/Geogaddi"},
        {"navidrome_id": "nd-s", "name": "One", "artist": "A", "song_count": 1, "folder": "_singles/A/One"},
    ])
    assert [a["navidrome_id"] for a in completeness.unmatched(lib_db.all_albums(), {})] == ["nd-u"]
    names = AsyncMock(return_value="rg-geo")
    tracks = AsyncMock(return_value=[{"title": str(i)} for i in range(23)])
    with patch("continuo.upstream.musicbrainz.release_group_by_names", names), \
         patch("continuo.upstream.musicbrainz.album_tracks", tracks):
        run(completeness.tick())
    names.assert_awaited_once_with("Boards of Canada", "Geogaddi")
    assert identity.album_matches()["nd-u"]["mb_release_group_id"] == "rg-geo"
    assert track_counts.all_counts() == {"rg-geo": 23}
    assert completeness.unmatched(lib_db.all_albums(), identity.album_matches()) == [], "looked up once"


def test_a_miss_isnt_looked_up_again_for_a_month(client):
    from continuo.db import identity
    lib_db.upsert_many([{"navidrome_id": "nd-u", "name": "X", "artist": "Y", "song_count": 3, "folder": "Y/X"}])
    identity.set_album_match("nd-u", None)
    assert completeness.unmatched(lib_db.all_albums(), identity.album_matches()) == []


def test_matching_by_name_wants_a_sure_score_and_the_same_artist(client):
    from continuo.upstream import musicbrainz
    answer = {"releases": [
        {"score": 100, "release-group": {"id": "rg-cover"}, "artist-credit": [{"name": "Tribute Band"}]},
        {"score": 98, "release-group": {"id": "rg-real"}, "artist-credit": [{"name": "Boards of Canada"}]},
    ]}
    with patch("continuo.upstream.musicbrainz._get", AsyncMock(return_value=answer)):
        assert run(musicbrainz.release_group_by_names("Boards of Canada", "Geogaddi")) == "rg-real"
    with patch("continuo.upstream.musicbrainz._get", AsyncMock(return_value={"releases": [
            {"score": 80, "release-group": {"id": "rg-meh"}, "artist-credit": [{"name": "Boards of Canada"}]}]})):
        assert run(musicbrainz.release_group_by_names("Boards of Canada", "Geogaddi")) is None
