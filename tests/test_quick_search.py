"""Quick search (searching while typing): the most listened matches, fast."""
import asyncio

import pytest

from continuo.search import merge
from continuo.upstream import lidarr, listenbrainz, musicbrainz, navidrome

SABRINA_C = "1882fe91-cdd9-49c9-9956-8e06a3810bd4"


@pytest.fixture
def upstreams(monkeypatch, fake):
    merge._quick_cache._data.clear()
    calls = {"mb": 0}

    async def mb_get(path, params, retries=2):
        calls["mb"] += 1
        return {"artists": [{"id": "s-1", "name": "Sabrina"}, {"id": SABRINA_C, "name": "Sabrina Carpenter"},
                            {"id": "t-1", "name": "[theatre]"}, {"id": "s-2", "name": "Sabrina Salerno"}]}

    async def lidarr_artists(q):
        return [{"kind": "artist", "name": "Sabrina", "musicBrainzArtistId": "s-1", "imageUrl": "https://images.lidarr.audio/s1.jpg",
                 "lidarrArtistId": None}]

    async def popularity(mbids):
        return {SABRINA_C: 1_310_072, "s-2": 83_030, "s-1": 2_718, "t-1": 2_015_889}

    async def top_albums(mbid, limit=5):
        return [{"musicBrainzReleaseGroupId": "rg-sns", "title": "Short n’ Sweet", "year": 2024, "albumType": "Album",
                 "caaReleaseMbid": "caa-1", "listens": 512_585}]

    async def top_songs(mbid, limit=10):
        return [{"title": "Espresso", "artist": "Sabrina Carpenter", "durationMs": 175_453, "recordingMbid": "rec-esp",
                 "releaseMbid": "rel-1", "releaseName": "Short n’ Sweet"}]

    async def library(q, user, artists=3, albums=3, songs=3):
        return {"artist": [], "album": [], "song": []}
    monkeypatch.setattr(musicbrainz, "_get", mb_get)
    monkeypatch.setattr(lidarr, "search_artists", lidarr_artists)
    monkeypatch.setattr(listenbrainz, "artist_popularity", popularity)
    monkeypatch.setattr(listenbrainz, "top_release_groups", top_albums)
    monkeypatch.setattr(listenbrainz, "top_recordings", top_songs)
    monkeypatch.setattr(navidrome, "search3", library)
    return calls


def test_the_most_listened_match_comes_first_and_brings_its_albums_and_songs(upstreams):
    out = asyncio.run(merge.quick_search("sabrina", "isaac"))
    artists = [r["name"] for r in out["results"] if r["kind"] == "artist"]
    assert artists[:3] == ["Sabrina Carpenter", "Sabrina Salerno", "Sabrina"], "by listens; [theatre] doesn't match"
    album = next(r for r in out["results"] if r["kind"] == "album")
    assert album["title"] == "Short n’ Sweet" and album["artist"] == "Sabrina Carpenter"
    song = next(r for r in out["results"] if r["kind"] == "track")
    assert song["key"] == "mb:rec:rec-esp" and song["album"]["musicBrainzReleaseGroupId"] == "rg-sns"
    assert out["partial"] is True


def test_a_prefix_typed_again_is_answered_from_the_cache(upstreams):
    asyncio.run(merge.quick_search("sabrina", "isaac"))
    asyncio.run(merge.quick_search("Sabrina", "isaac"))
    assert upstreams["mb"] == 1


def test_a_slow_musicbrainz_doesn_t_hold_the_quick_search_up(upstreams, monkeypatch):
    async def slow(path, params, retries=2):
        await asyncio.sleep(5)
    monkeypatch.setattr(musicbrainz, "_get", slow)
    monkeypatch.setattr(merge, "_QUICK_MB_BUDGET_S", 0.05)
    out = asyncio.run(merge.quick_search("sabrina", "isaac"))
    assert [r["name"] for r in out["results"] if r["kind"] == "artist"] == ["Sabrina"], "Lidarr's candidates alone"


def test_a_tracklist_is_one_musicbrainz_call(monkeypatch):
    musicbrainz._tracklist_cache.clear()
    asked = []

    async def get(path, params, retries=2):
        asked.append(path)
        return {"releases": [
            {"id": "rel-late", "status": "Official", "date": "2025-01-01", "media": []},
            {"id": "rel-first", "status": "Official", "date": "2024-08-23",
             "media": [{"position": 1, "tracks": [{"position": 1, "title": "Taste", "length": 157_000, "recording": {"id": "rec-t"}}]}]}]}
    monkeypatch.setattr(musicbrainz, "_get", get)
    tracks = asyncio.run(musicbrainz.album_tracks("rg-sns"))
    assert asked == ["/release"] and tracks[0]["title"] == "Taste" and tracks[0]["musicBrainzRecordingId"] == "rec-t"


def test_a_slow_listenbrainz_doesn_t_hold_the_quick_search_up_nor_stay_cached(upstreams, monkeypatch):
    async def hangs(mbid, limit=5):
        await asyncio.sleep(5)
    monkeypatch.setattr(listenbrainz, "top_recordings", hangs)
    monkeypatch.setattr(merge, "_QUICK_LB_BUDGET_S", 0.05)
    out = asyncio.run(merge.quick_search("sabrina", "isaac"))
    kinds = [r["kind"] for r in out["results"]]
    assert "artist" in kinds and "album" in kinds and "track" not in kinds, "the songs left out, the rest there"
    asyncio.run(merge.quick_search("sabrina", "isaac"))
    assert upstreams["mb"] == 2, "a cut-short result is asked again next time"


def test_the_fallback_quick_search_is_in_its_order_too(upstreams):
    out = asyncio.run(merge.quick_search("sabrina", "isaac"))
    assert out["ranked"] is True, "most listened first: the app keeps the order"
