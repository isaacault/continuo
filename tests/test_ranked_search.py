"""Search ranked by what people listen to (search.ranked): Deezer finds and ranks, ListenBrainz
and Lidarr give the hits MusicBrainz IDs."""
import asyncio

import pytest

from continuo.search import merge, ranked
from continuo.upstream import deezer, lidarr, listenbrainz


def test_names_match_whole_by_start_by_words_or_not():
    assert ranked.match("weeknd", "The Weeknd") == 3.0          # "The" aside
    assert ranked.match("sabr", "Sabrina Carpenter") == 2.0
    assert ranked.match("tyler creator", "Tyler, The Creator") == 1.5
    assert ranked.match("humble", "HUMBLE.") == 3.0
    assert ranked.match("chappell", "Good Luck, Babe!") == 0.0


def test_a_song_matches_by_title_or_title_and_artist_not_its_artist_alone():
    assert ranked.song_match("kendrick humble", "HUMBLE.", "Kendrick Lamar") > 0
    assert ranked.song_match("espresso", "Espresso", "Sabrina Carpenter") == 3.0
    assert ranked.song_match("chappell", "Good Luck, Babe!", "Chappell Roan") == 0.0


def test_a_much_bigger_name_beats_a_slightly_closer_one():
    carpenter = ranked.score(ranked.match("sabrina", "Sabrina Carpenter"), 6.7)   # millions of fans
    sabrina = ranked.score(ranked.match("sabrina", "Sabrina"), 3.5)               # a few thousand
    assert carpenter > sabrina
    assert ranked.score(0.0, 9.0) == 0.0, "popular but not what was typed: doesn't lead"


def test_the_top_songs_add_artists_deezer_s_artist_search_buried():
    tracks = [{"title": "Cruel Summer", "artist": "Taylor Swift", "album": "Lover", "rank": 950_000, "cover": None, "durationMs": 1}]
    artists, _albums = ranked.pools("taylor", tracks, [{"name": "Taylor & Close", "fans": 900, "picture": None}], [])
    assert {a["name"] for a in artists} == {"Taylor & Close", "Taylor Swift"}


@pytest.fixture
def upstreams(monkeypatch, fake):
    ranked._ids_cache.clear()

    async def tracks(q, limit=25):
        return [{"title": "Espresso", "artist": "Sabrina Carpenter", "album": "Short n' Sweet", "durationMs": 175_000,
                 "rank": 947_013, "cover": "https://cdn-images.dzcdn.net/images/cover/x/500x500.jpg"},
                {"title": "Espresso", "artist": "Laza Bossa", "album": "Cafe", "durationMs": 200_000, "rank": 783_916, "cover": None}]

    async def artists(q, limit=10):
        return [{"name": "Espresso", "fans": 40, "picture": None}]

    async def albums(q, limit=10):
        return [{"title": "Espresso", "artist": "Sabrina Carpenter", "recordType": "single", "cover": None}]

    async def lookup(pairs):
        return [{"recordingMbid": f"rec-{i}", "recordingName": t, "artistCreditName": a, "artistMbids": [f"art-{i}"],
                 "releaseMbid": f"rel-{i}", "releaseName": "x"} for i, (a, t) in enumerate(pairs)]

    async def releases(ids):
        return {m: {"releaseGroupMbid": f"rg-{m}", "name": "Short n' Sweet", "year": 2024, "caaReleaseMbid": None} for m in ids}

    async def artist_mbid(name):
        return [{"name": name, "musicBrainzArtistId": f"mb-{name}"}]

    async def album_lookup(term):
        return [{"title": "Espresso", "artist": "Sabrina Carpenter", "musicBrainzReleaseGroupId": "rg-single",
                 "musicBrainzArtistId": "mb-sc", "year": 2024, "albumType": "Single"}]

    async def rg_listens(ids):
        return {i: 50_000 for i in ids}
    monkeypatch.setattr(deezer, "search_tracks", tracks)
    monkeypatch.setattr(deezer, "search_artists", artists)
    monkeypatch.setattr(deezer, "search_albums", albums)
    monkeypatch.setattr(listenbrainz, "lookup_recordings", lookup)
    monkeypatch.setattr(listenbrainz, "recording_releases", releases)
    monkeypatch.setattr(listenbrainz, "release_group_popularity", rg_listens)
    monkeypatch.setattr(lidarr, "search_artists", artist_mbid)
    monkeypatch.setattr(lidarr, "search_albums", album_lookup)


def test_a_song_title_leads_with_the_popular_song_not_an_artist_or_its_single(upstreams):
    out = asyncio.run(ranked.catalog("espresso", 3))
    assert out["best"] == "mb:rec:rec-0", "Sabrina Carpenter's Espresso"
    song = out["tracks"][0]
    assert song["title"] == "Espresso" and song["album"]["musicBrainzReleaseGroupId"] == "rg-rec-0"
    assert song["imageUrl"].startswith("https://cdn-images.dzcdn.net/")


def test_ids_are_kept_so_the_next_keystroke_asks_less(upstreams, monkeypatch):
    asyncio.run(ranked.catalog("espresso", 3))
    asked = []

    async def lookup(pairs):
        asked.append(pairs)
        return [None] * len(pairs)
    monkeypatch.setattr(listenbrainz, "lookup_recordings", lookup)
    asyncio.run(ranked.catalog("espresso", 3))
    assert asked == [], "both songs' IDs came from the cache"


def test_your_own_artist_leads_when_its_name_starts_so():
    results = [{"kind": "artist", "key": "mb:artist:x", "name": "Maverick City Music", "availability": {"state": "catalog"}},
               {"kind": "artist", "key": "nd:artist:1", "name": "Maverick Sabre", "availability": {"state": "in_library"}},
               {"kind": "track", "key": "nd:track:2", "title": "Maverick Love", "availability": {"state": "in_library"}}]
    assert merge.lead("maverick", results, "mb:artist:x") == "nd:artist:1"
    assert merge.lead("mav", results[:1] + results[2:], "mb:artist:x") == "mb:artist:x", "a song of yours leads only by its exact title"


def test_without_deezer_quick_search_still_answers(upstreams, monkeypatch):
    async def exhausted(q, limit=25):
        raise deezer.Exhausted()
    monkeypatch.setattr(deezer, "search_tracks", exhausted)
    assert asyncio.run(merge._ranked("espresso", 3)) is None, "the caller falls back to MusicBrainz's search"


def test_continuo_keeps_inside_deezer_s_rate_limit():
    deezer.reset()
    for _ in range(deezer._BUDGET):
        deezer._spend()
    with pytest.raises(deezer.Exhausted):
        deezer._spend()
    deezer.reset()


def test_a_ranked_search_says_so(upstreams, monkeypatch):
    from continuo.upstream import navidrome

    async def library(q, user, artists=3, albums=3, songs=3):
        return {"artist": [], "album": [], "song": []}
    monkeypatch.setattr(navidrome, "search3", library)
    out = asyncio.run(merge.quick_search("espresso", "isaac"))
    assert out["ranked"] is True and out["best"] == "mb:rec:rec-0"
