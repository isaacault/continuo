"""Carrying ratings, stars and playlists to an account's own library (#27)."""
import asyncio
import dataclasses
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from continuo import config
from continuo.db import library as lib_db
from continuo.library import carry

ND = "http://navidrome.test"


@pytest.fixture
def nd(fake, monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, navidrome_auth="header",
                                                                navidrome_user_header="Remote-User"))
    lib_db.upsert_many([
        {"navidrome_id": "a-1", "name": "Geogaddi", "folder": "BoC/Geogaddi", "norm_key": "a", "library_id": 1},
        {"navidrome_id": "a-2", "name": "Geogaddi", "folder": "BoC/Geogaddi", "norm_key": "a", "library_id": 2},
    ])
    songs = [
        {"id": "s-1", "path": "BoC/Geogaddi/01.flac", "libraryId": 1, "rating": 5, "starred": True},
        {"id": "s-2", "path": "BoC/Geogaddi/02.flac", "libraryId": 1},
        {"id": "s-3", "path": "Gone/Old/01.flac", "libraryId": 1},          # not in the new library
        {"id": "n-1", "path": "BoC/Geogaddi/01.flac", "libraryId": 2},
        {"id": "n-2", "path": "BoC/Geogaddi/02.flac", "libraryId": 2},
    ]
    albums = [{"id": "a-1", "starred": True}, {"id": "a-2"}]
    sent = []

    def native(req: httpx.Request):
        assert req.headers.get("remote-user") == "isaac", "read as the account, for its own ratings"
        data = songs if req.url.path.endswith("/song") else albums
        return httpx.Response(200, json=data, headers={"x-total-count": str(len(data))})

    def rest(req: httpx.Request):
        name = req.url.path.rsplit("/", 1)[1]
        q = parse_qs(urlparse(str(req.url)).query)
        if name == "getPlaylists":
            pl = [{"id": "p-1", "name": "Cassette · Night", "owner": "isaac"},
                  {"id": "p-2", "name": "Someone else's", "owner": "guest"}]
            return httpx.Response(200, json={"subsonic-response": {"status": "ok", "playlists": {"playlist": pl}}})
        if name == "getPlaylist":
            entries = [{"id": "s-2"}, {"id": "s-3"}, {"id": "s-1"}]
            return httpx.Response(200, json={"subsonic-response": {"status": "ok", "playlist": {"entry": entries}}})
        sent.append((name, {k: v for k, v in q.items() if k not in ("c", "f", "v")}))
        return httpx.Response(200, json={"subsonic-response": {"status": "ok"}})
    fake.on("GET", f"{ND}/api/song", handler=native)
    fake.on("GET", f"{ND}/api/album", handler=native)
    for name in ("getPlaylists", "getPlaylist", "setRating", "star", "createPlaylist"):
        fake.on("GET", f"{ND}/rest/{name}", handler=rest)
    return sent


def test_a_dry_run_counts_and_changes_nothing(nd):
    out = asyncio.run(carry.carry("isaac", 2, dry_run=True))
    assert out == {"songs": 2, "unmatched_songs": 1, "ratings": 1, "stars": 2, "playlists": 1,
                   "playlist_tracks": 2, "unmatched_tracks": 1}
    assert nd == []


def test_ratings_stars_and_playlists_move_to_the_own_library(nd):
    asyncio.run(carry.carry("isaac", 2))
    by = {name: q for name, q in nd}
    assert by["setRating"] == {"id": ["n-1"], "rating": ["5"]}
    assert sorted(by["star"]["id"]) == ["a-2", "n-1"]
    assert by["createPlaylist"] == {"playlistId": ["p-1"], "songId": ["n-2", "s-3", "n-1"]}, \
        "same order; a track the new library lacks stays as it was"
