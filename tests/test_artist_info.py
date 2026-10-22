"""GET /v1/catalog/artists/{mbid}: the catalogue artist page (picture, biography, top songs,
similar artists), assembled from Lidarr's metadata server, ListenBrainz and Wikipedia."""
import dataclasses
from urllib.parse import quote

import pytest
from conftest import LIDARR, bearer, login, make_user

from continuo import config
from continuo.db import library as lib_db
from continuo.requests.normalise import album_key
from continuo.search import artist_info

ART = "69158f97-4c07-4c4e-baf8-4e4ab1ed666e"
RG = "4a1b2c3d-0000-4000-8000-000000000001"
RG2 = "4a1b2c3d-0000-4000-8000-000000000002"
REL = "5b2c3d4e-0000-4000-8000-000000000009"
APHEX = "f22942a1-6f70-4f48-866e-238cb2308fbd"
POSTER = "https://images.lidarr.audio/cache/https://assets.fanart.tv/fanart/boc.jpg"
META = f"https://api.lidarr.audio/api/v0.4/artist/{ART}"
LB_META = "https://api.listenbrainz.org/1/metadata/artist/"
LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
LB_TOP = f"https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/{ART}"
WIKIDATA = "https://www.wikidata.org/wiki/Special:EntityData/Q722778.json"
WIKIPEDIA = "https://en.wikipedia.org/api/rest_v1/page/summary/Boards_of_Canada"


@pytest.fixture(autouse=True)
def _fresh_cache():
    artist_info._cache.clear()
    yield
    artist_info._cache.clear()


def _token(monkeypatch, token="lb-token"):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, listenbrainz_token=token))


def _upstreams(fake, overview=""):
    fake.on("GET", META, {
        "artistname": "Boards of Canada", "overview": overview, "type": "Group", "genres": ["electronic"],
        "images": [{"CoverType": "Fanart", "Url": "https://images.lidarr.audio/cache/f.jpg"},
                   {"CoverType": "Poster", "Url": POSTER}],
        "Albums": [{"Id": RG, "Title": "Geogaddi", "Type": "Album", "ReleaseStatuses": ["Official"]},
                   {"Id": RG2, "Title": "Tomorrow's Harvest", "Type": "Album", "ReleaseStatuses": ["Official"]}],
    })
    fake.on("GET", LB_META, [{"name": "Boards of Canada", "rels": {"wikidata": "https://www.wikidata.org/wiki/Q722778"}}])
    fake.on("GET", LB_SIMILAR, [
        {"artist_mbid": APHEX, "name": "Aphex Twin", "comment": ""},
        {"artist_mbid": "9a709693-b4f8-4da9-8cc1-038c911a61be", "name": "Bonobo", "comment": "UK electronic artist"},
    ])
    fake.on("GET", WIKIDATA, {"entities": {"Q722778": {"sitelinks": {"enwiki": {"title": "Boards of Canada"}}}}})
    fake.on("GET", WIKIPEDIA, {"extract": "Boards of Canada are a Scottish electronic music duo.",
                               "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Boards_of_Canada"}}})
    fake.on("GET", f"{LIDARR}/artist", [])


def test_page_without_a_token_has_everything_but_top_songs(client, fake):
    make_user()
    s = login(client)
    _upstreams(fake)
    lib_db.upsert_many([{"navidrome_id": "nd-9", "name": "Syro", "artist": "Aphex Twin",
                         "norm_key": album_key("Aphex Twin", "Syro")}])

    r = client.get(f"/v1/catalog/artists/{ART}", headers=bearer(s))
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["name"] == "Boards of Canada"
    assert page["artwork"] == {"url": f"https://music-api.test/v1/artwork?src={quote(POSTER, safe='')}"}
    assert page["biography"] == {"text": "Boards of Canada are a Scottish electronic music duo.",
                                 "source": "wikipedia", "url": "https://en.wikipedia.org/wiki/Boards_of_Canada"}
    assert [a["name"] for a in page["similarArtists"]] == ["Aphex Twin", "Bonobo"]
    assert page["similarArtists"][0]["availability"] == {"state": "in_library"}
    assert page["similarArtists"][1]["availability"] == {"state": "catalog"}
    assert page["topTracks"] == []
    assert page["sources"] == {"topTracks": "needs_token", "similarArtists": "ok"}
    assert not fake.called("GET", LB_TOP), "no token, no popularity call"


def test_lidarr_overview_wins_over_wikipedia(client, fake):
    make_user()
    s = login(client)
    _upstreams(fake, overview="From Lidarr.")
    page = client.get(f"/v1/catalog/artists/{ART}", headers=bearer(s)).json()
    assert page["biography"] == {"text": "From Lidarr.", "source": "lidarr", "url": None}
    assert not fake.called("GET", WIKIPEDIA)


def test_top_songs_are_matched_to_albums_and_the_library(client, fake, monkeypatch):
    _token(monkeypatch)
    make_user()
    s = login(client)
    _upstreams(fake)
    fake.on("GET", LB_TOP, [
        {"recording_name": "Dayvan Cowboy", "artist_name": "Boards of Canada", "length": 300000,
         "recording_mbid": "r1", "release_mbid": REL, "release_name": "Geogaddi"},
        {"recording_name": "Reach for the Dead", "artist_name": "Boards of Canada", "length": 286000,
         "recording_mbid": "r2", "release_mbid": "other", "release_name": "Tomorrow’s Harvest"},
        {"recording_name": "Rare B-side", "artist_name": "Boards of Canada", "recording_mbid": "r3",
         "release_name": "Some Bootleg"},
    ])
    lib_db.upsert_many([{"navidrome_id": "nd-1", "name": "Geogaddi", "artist": "Boards of Canada",
                         "mbz_album_id": REL, "norm_key": album_key("Boards of Canada", "Geogaddi")}])

    page = client.get(f"/v1/catalog/artists/{ART}", headers=bearer(s)).json()
    owned, missing, unknown = page["topTracks"]
    assert owned["availability"] == {"state": "album_in_library", "navidromeAlbumId": "nd-1"}
    assert owned["album"]["navidromeAlbumId"] == "nd-1"
    assert missing["album"]["musicBrainzReleaseGroupId"] == RG2
    assert missing["availability"] == {"state": "requestable", "via": "album"}
    assert unknown["availability"] == {"state": "catalog"}
    assert page["sources"]["topTracks"] == "ok"
    assert fake.called("GET", LB_TOP)[0].headers["Authorization"] == "Token lb-token"


def test_listenbrainz_down_still_gives_a_page(client, fake):
    make_user()
    s = login(client)
    _upstreams(fake)
    fake.on("GET", LB_SIMILAR, {"error": "boom"}, status=503)
    page = client.get(f"/v1/catalog/artists/{ART}", headers=bearer(s)).json()
    assert page["name"] == "Boards of Canada"
    assert page["similarArtists"] == []
    assert page["sources"]["similarArtists"] == "unavailable"


def test_unknown_artist_is_404_and_not_cached(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", META, {"error": "not found"}, status=404)
    fake.on("GET", LB_META, [])
    fake.on("GET", LB_SIMILAR, [])
    r = client.get(f"/v1/catalog/artists/{ART}", headers=bearer(s))
    assert r.status_code == 404
    assert artist_info._cache.get(ART) is None


def test_bad_mbid_is_400(client, fake):
    make_user()
    s = login(client)
    assert client.get("/v1/catalog/artists/not-an-id", headers=bearer(s)).status_code == 400
