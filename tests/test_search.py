from conftest import LIDARR, ND, bearer, login, make_user

from continuo.db import library as lib_db
from continuo.db import requests as req_db
from continuo.requests.normalise import album_key

GEO = "4a1b2c3d-0000-4000-8000-000000000001"
HARVEST = "4a1b2c3d-0000-4000-8000-000000000002"


def _search3(fake, albums=(), artists=(), songs=()):
    fake.on("GET", f"{ND}/rest/search3", {"subsonic-response": {"status": "ok", "searchResult3": {
        "album": list(albums), "artist": list(artists), "song": list(songs)}}})


def _lookup(fake, albums):
    fake.on("GET", f"{LIDARR}/album/lookup", albums)
    fake.on("GET", f"{LIDARR}/album", [])


def test_library_wins_and_catalogue_folds_in(client, fake):
    make_user()
    s = login(client)
    _search3(fake, albums=[{"id": "nd-geo", "name": "Geogaddi", "artist": "Boards of Canada",
                            "year": 2002, "songCount": 23, "coverArt": "al-nd-geo"}])
    _lookup(fake, [
        {"foreignAlbumId": GEO, "title": "Geogaddi", "releaseDate": "2002-02-18",
         "albumType": "Album", "artist": {"artistName": "Boards of Canada"}, "images": []},
        {"foreignAlbumId": HARVEST, "title": "Tomorrow’s Harvest", "releaseDate": "2013-06-05",
         "albumType": "Album", "artist": {"artistName": "Boards of Canada"}, "images": []},
    ])
    r = client.get("/v1/search", params={"q": "boards of canada", "types": "album"},
                   headers=bearer(s))
    body = r.json()
    assert body["sources"] == {"library": "ok", "catalog": "ok"}
    geo, harvest = body["results"]
    assert geo["key"] == f"mb:rg:{GEO}" and geo["availability"]["state"] == "in_library"
    assert geo["ids"]["navidromeAlbumId"] == "nd-geo"
    assert geo["artwork"]["url"].endswith("/rest/getCoverArt?id=al-nd-geo")
    assert harvest["availability"] == {"state": "requestable"}
    # Catalogue art goes through continuo (issue #13), library art stays Subsonic.
    assert harvest["artwork"] == {"url": "https://music-api.test/v1/artwork?src="
                                  "https%3A%2F%2Fcoverartarchive.org%2Frelease-group%2F"
                                  f"{HARVEST}%2Ffront-500"}
    assert geo["artwork"]["auth"] == "subsonic"
    # search3 ran as the user (Remote-User), not the service account.
    nd_call = fake.called("GET", f"{ND}/rest/search3")[0]
    assert nd_call.headers["remote-user"] == "isaac"


def test_curly_quote_catalogue_hit_folds_into_library_album(client, fake):
    """fix/88 regression: ’ vs ' must not produce two rows."""
    make_user()
    s = login(client)
    lib_db.upsert_many([{"navidrome_id": "nd-h", "name": "Tomorrow's Harvest",
                         "artist": "Boards of Canada", "year": 2013, "song_count": 17,
                         "mbz_album_id": None, "mbz_release_group_id": None, "folder": None,
                         "norm_key": album_key("Boards of Canada", "Tomorrow's Harvest"),
                         "source_updated_at": None}])
    _search3(fake)
    _lookup(fake, [{"foreignAlbumId": HARVEST, "title": "Tomorrow’s Harvest",
                    "artist": {"artistName": "Boards of Canada"}, "images": []}])
    results = client.get("/v1/search", params={"q": "harvest", "types": "album"},
                         headers=bearer(s)).json()["results"]
    assert len(results) == 1
    assert results[0]["availability"]["state"] == "in_library"
    assert results[0]["ids"] == {"musicBrainzReleaseGroupId": HARVEST,
                                 "musicBrainzReleaseId": None, "navidromeAlbumId": "nd-h",
                                 "lidarrAlbumId": None}


def test_requested_and_partial_states(client, fake):
    make_user()
    s = login(client)
    row = req_db.create(mb_release_group_id=HARVEST, title="Tomorrow's Harvest", artist="BoC",
                        year=None, artwork_url=None, user_id=1)
    req_db.update(row["id"], status="downloading")
    _search3(fake, albums=[{"id": "nd-geo", "name": "Geogaddi", "artist": "BoC", "songCount": 9}])
    fake.on("GET", f"{LIDARR}/album/lookup", [
        {"id": 5, "foreignAlbumId": GEO, "title": "Geogaddi", "artist": {"artistName": "BoC"}},
        {"foreignAlbumId": HARVEST, "title": "Tomorrow's Harvest", "artist": {"artistName": "BoC"}},
    ])
    fake.on("GET", f"{LIDARR}/album", [{"id": 5, "statistics": {"trackFileCount": 9,
                                                                "trackCount": 23}}])
    geo, harvest = client.get("/v1/search", params={"q": "boc", "types": "album"},
                              headers=bearer(s)).json()["results"]
    assert geo["availability"] == {"state": "partial", "have": 9, "expected": 23,
                                   "navidromeAlbumId": "nd-geo"}
    assert harvest["availability"] == {"state": "downloading", "requestId": row["id"]}


def test_catalogue_down_is_degraded_not_an_error(client, fake):
    make_user()
    s = login(client)
    _search3(fake, albums=[{"id": "nd-geo", "name": "Geogaddi", "artist": "BoC"}])
    fake.on("GET", f"{LIDARR}/album/lookup", {}, status=503)
    fake.on("GET", f"{LIDARR}/artist/lookup", [{"artistName": "Boards of Canada",
                                               "foreignArtistId": "a1"}])
    body = client.get("/v1/search", params={"q": "boc", "types": "album,artist"},
                      headers=bearer(s)).json()
    assert body["sources"]["catalog"] == "degraded"
    kinds = [r["kind"] for r in body["results"]]
    assert kinds == ["album", "artist"]
    assert body["results"][1]["availability"] == {"state": "catalog"}


def test_tracks_point_at_their_album(client, fake):
    make_user()
    s = login(client)
    _search3(fake, songs=[{"id": "nd-t1", "title": "Music Is Math", "artist": "Boards of Canada",
                           "album": "Geogaddi", "albumId": "nd-geo", "duration": 321}])
    fake.on("GET", "https://musicbrainz.org/ws/2/recording", {"recordings": [
        {"id": "rec-1", "title": "Music Is Math", "artist-credit": [{"name": "Boards of Canada"}],
         "releases": [{"release-group": {"id": GEO, "primary-type": "Album", "title": "Geogaddi"}}]},
        {"id": "rec-2", "title": "Dayvan Cowboy", "artist-credit": [{"name": "Boards of Canada"}],
         "releases": [{"release-group": {"id": HARVEST, "primary-type": "EP",
                                         "title": "Trans Canada Highway"}}]},
    ]})
    results = client.get("/v1/search", params={"q": "boc", "types": "track"},
                         headers=bearer(s)).json()["results"]
    assert len(results) == 2
    assert results[0]["availability"] == {"state": "in_library", "navidromeTrackId": "nd-t1"}
    assert results[0]["album"]["musicBrainzReleaseGroupId"] == GEO
    assert results[1]["availability"] == {"state": "requestable", "via": "album"}
    ua = fake.called("GET", "https://musicbrainz.org/ws/2/recording")[0].headers["user-agent"]
    assert ua.startswith("continuo/")
