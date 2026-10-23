"""GET /v1/catalog/artists/resolve: which same-name catalogue artist a library artist is."""
import httpx
import pytest
from conftest import LIDARR, bearer, login, make_user

from continuo.db import library as lib_db
from continuo.requests.normalise import album_key
from continuo.search import resolve

CROATIAN = "660ad6f4-9a86-4d3c-9305-f269186b195c"
CANADIAN = "1888892a-06b7-4bbd-b0a1-3ffde01e4094"
DETROIT = "3ac96bce-83c9-4738-afb2-49281f343236"


@pytest.fixture(autouse=True)
def _fresh():
    resolve._facts_cache.clear()
    yield


def _peach_pits(fake, in_lidarr=None):
    fake.on("GET", f"{LIDARR}/artist/lookup", [
        {"foreignArtistId": CROATIAN, "artistName": "Peach Pit"},
        {"foreignArtistId": CANADIAN, "artistName": "Peach Pit"},
        {"foreignArtistId": DETROIT, "artistName": "Peach Pit"},
        {"foreignArtistId": "other", "artistName": "Peaches"},
    ])
    fake.on("GET", f"{LIDARR}/artist", handler=lambda req: httpx.Response(
        200, json=[{"foreignArtistId": in_lidarr}] if in_lidarr and req.url.params.get("mbId") == in_lidarr else []))
    for mbid, titles in ((CROATIAN, ["Rumble"]), (CANADIAN, ["Being So Normal", "You and Your Friends"]), (DETROIT, ["Detroit"])):
        fake.on("GET", f"https://api.lidarr.audio/api/v0.4/artist/{mbid}",
                {"artistname": "Peach Pit", "Albums": [{"Title": t, "Type": "Album"} for t in titles]})
    lib_db.upsert_many([{"navidrome_id": "nd-1", "name": "Being So Normal", "artist": "Peach Pit",
                         "norm_key": album_key("Peach Pit", "Being So Normal")}])


def test_the_artist_sharing_the_library_s_albums_wins(client, fake):
    make_user()
    s = login(client)
    _peach_pits(fake)
    r = client.get("/v1/catalog/artists/resolve", params={"name": "Peach Pit"}, headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json() == {"musicBrainzArtistId": CANADIAN, "name": "Peach Pit", "sharedAlbums": 1, "inLidarr": False}


def test_an_artist_already_in_lidarr_wins(client, fake):
    make_user()
    s = login(client)
    _peach_pits(fake, in_lidarr=DETROIT)
    r = client.get("/v1/catalog/artists/resolve", params={"name": "peach pit"}, headers=bearer(s))
    assert r.json()["musicBrainzArtistId"] == DETROIT and r.json()["inLidarr"] is True


def test_no_same_name_artist_is_404(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", f"{LIDARR}/artist/lookup", [{"foreignArtistId": "other", "artistName": "Peaches"}])
    assert client.get("/v1/catalog/artists/resolve", params={"name": "Peach Pit"}, headers=bearer(s)).status_code == 404


def test_a_lone_candidate_with_another_name_and_nothing_shared_is_not_the_artist(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", f"{LIDARR}/artist/lookup", [{"foreignArtistId": "pp-tribute", "artistName": "Peach Pit Tribute Band"}])
    fake.on("GET", "https://api.lidarr.audio/api/v0.4/artist/pp-tribute", {"artistname": "Peach Pit Tribute Band", "Albums": []})
    assert client.get("/v1/catalog/artists/resolve", params={"name": "Peach Pit"}, headers=bearer(s)).status_code == 404
