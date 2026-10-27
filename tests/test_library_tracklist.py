"""A library album's whole tracklist, for showing what the library doesn't have of it."""
from conftest import bearer, login, make_user

from continuo.db import library as lib_db
from continuo.upstream import musicbrainz


def _album(nd_id, rg):
    lib_db.upsert_many([{"navidrome_id": nd_id, "name": "To Cy & Lee: Instrumentals, Vol. 1", "artist": "Alabaster dePlume",
                         "mbz_album_id": None, "mbz_release_group_id": rg, "year": 2020, "song_count": 1,
                         "folder": "_singles/Alabaster dePlume/To Cy & Lee_ Instrumentals, Vol. 1"}])


def test_a_single_in_the_library_brings_its_whole_album(client, monkeypatch):
    make_user()
    s = login(client)
    _album("nd-cy", "rg-cy")
    asked = []

    async def tracks(rg, release_mbid=None, strict=False):
        asked.append(rg)
        return [{"position": 1, "title": "Visit Croatia", "durationMs": 1, "discNumber": 1, "musicBrainzRecordingId": "r1"},
                {"position": 2, "title": "Song of Gratitude", "durationMs": 1, "discNumber": 1, "musicBrainzRecordingId": "r2"}]
    monkeypatch.setattr(musicbrainz, "album_tracks", tracks)
    r = client.get("/v1/library/albums/nd-cy/tracklist", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json()["musicBrainzReleaseGroupId"] == "rg-cy" and [t["title"] for t in r.json()["tracks"]] == ["Visit Croatia", "Song of Gratitude"]
    assert asked == ["rg-cy"]


def test_an_album_without_a_release_group_has_none(client):
    make_user()
    s = login(client)
    _album("nd-x", None)
    assert client.get("/v1/library/albums/nd-x/tracklist", headers=bearer(s)).status_code == 404
    assert client.get("/v1/library/albums/nd-unknown/tracklist", headers=bearer(s)).status_code == 404
