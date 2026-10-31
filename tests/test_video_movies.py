"""The video part's film endpoints and request pipeline (docs/design/video.md §7, §11 step 4)."""
import os

import httpx
import pytest
from conftest import bearer, login, make_user, run

from continuo import config

JELLYFIN = "http://jellyfin.test"
RADARR = "http://radarr.test/api/v3"
TMDB = "https://api.themoviedb.org/3"
MATRIX = {"id": 603, "title": "The Matrix", "release_date": "1999-03-31", "poster_path": "/m.jpg",
          "backdrop_path": "/b.jpg", "runtime": 136, "overview": "Neo.", "genres": [{"name": "Action"}],
          "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "vKQi3bBA1y8"}]}}


@pytest.fixture
def vclient(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf",
                       RADARR_URL="http://radarr.test", RADARR_API_KEY="rd", TMDB_API_KEY="tm"))
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def _jellyfin(fake, items):
    fake.on("GET", f"{JELLYFIN}/Items", {"Items": items})


def test_search_marks_what_is_in_the_library_and_adds_library_only_titles(vclient, fake):
    from continuo.video import index
    _jellyfin(fake, [{"Id": "jf-1", "Name": "The Matrix", "Type": "Movie", "ProductionYear": 1999, "ProviderIds": {"Tmdb": "603"}},
                     {"Id": "jf-h", "Name": "Matrix of home videos", "Type": "Movie", "ProviderIds": {}}])
    run(index.refresh())
    fake.on("GET", f"{TMDB}/search/multi", {"results": [
        {"id": 603, "media_type": "movie", "title": "The Matrix", "release_date": "1999-03-31", "poster_path": "/m.jpg"},
        {"id": 604, "media_type": "movie", "title": "The Matrix Reloaded", "release_date": "2003-05-15"}]})
    make_user()
    s = login(vclient)
    r = vclient.get("/v1/video/search", params={"q": "matrix"}, headers=bearer(s))
    assert r.status_code == 200, r.text
    out = r.json()["results"]
    assert [(h["tmdbId"], h["availability"]["state"]) for h in out] == [(603, "in_library"), (604, "requestable"), (None, "in_library")]
    assert out[0]["availability"]["jellyfinId"] == "jf-1"
    assert out[0]["poster"].endswith("/v1/artwork?src=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw500%2Fm.jpg")


def test_a_film_page(vclient, fake):
    fake.on("GET", f"{TMDB}/movie/603", MATRIX)
    make_user()
    s = login(vclient)
    m = vclient.get("/v1/video/movies/603", headers=bearer(s)).json()
    assert (m["title"], m["year"], m["runtimeMinutes"], m["genres"], m["trailerYouTubeKey"]) == \
        ("The Matrix", 1999, 136, ["Action"], "vKQi3bBA1y8")
    assert m["availability"] == {"state": "requestable"} and "w1280" in m["backdrop"]


def test_a_film_request_goes_to_radarr_and_ends_available_with_a_notification(vclient, fake):
    from continuo.db import notifications as notif_db
    from continuo.video import db, requests
    fake.on("GET", f"{TMDB}/movie/603", MATRIX)
    fake.on("GET", f"{RADARR}/movie", [])
    fake.on("GET", f"{RADARR}/movie/lookup/tmdb", {"tmdbId": 603, "title": "The Matrix"})
    fake.on("POST", f"{RADARR}/movie", handler=lambda r: httpx.Response(201, json={"id": 9, "tmdbId": 603}))
    make_user()
    s = login(vclient)
    r = vclient.post("/v1/video/requests", json={"kind": "movie", "tmdbId": 603}, headers=bearer(s))
    assert r.status_code == 201, r.text
    rid = r.json()["request"]["id"]
    assert r.json()["request"]["title"] == "The Matrix" and r.json()["created"] is True
    run(requests.start(rid))
    assert db.get(rid)["status"] == "searching" and db.get(rid)["radarr_movie_id"] == 9
    again = vclient.post("/v1/video/requests", json={"kind": "movie", "tmdbId": 603}, headers=bearer(s)).json()
    assert again["created"] is False and again["request"]["id"] == rid, "one request per film"
    # downloading
    fake.on("GET", f"{RADARR}/movie", [{"id": 9, "tmdbId": 603, "hasFile": False}])
    fake.on("GET", f"{RADARR}/queue/details", [{"movieId": 9, "sizeleft": 1}])
    _jellyfin(fake, [])
    run(requests.tick())
    assert db.get(rid)["status"] == "downloading"
    # Radarr has it, Jellyfin has it: available, the owner told
    fake.on("GET", f"{RADARR}/movie", [{"id": 9, "tmdbId": 603, "hasFile": True}])
    fake.on("GET", f"{RADARR}/queue/details", [])
    _jellyfin(fake, [{"Id": "jf-1", "Name": "The Matrix", "Type": "Movie", "ProviderIds": {"Tmdb": "603"}}])
    run(requests.tick())
    done = db.get(rid)
    assert done["status"] == "available" and done["jellyfin_id"] == "jf-1" and done["notified_at"]
    n = notif_db.list_for_user(1)[0]
    assert n["type"] == "video_request_available" and n["body"] == "The Matrix (1999)"
    assert n["action"]["tmdbId"] == 603 and n["action"]["deepLink"] == "watch://movie/603"
    listed = vclient.get("/v1/video/requests", headers=bearer(s)).json()["requests"]
    assert listed[0]["status"] == "available"


def test_requests_refused_or_failing(vclient, fake):
    from continuo.video import db, index, requests
    _jellyfin(fake, [{"Id": "jf-1", "Name": "The Matrix", "Type": "Movie", "ProviderIds": {"Tmdb": "603"}}])
    run(index.refresh())
    make_user()
    s = login(vclient)
    assert vclient.post("/v1/video/requests", json={"kind": "movie", "tmdbId": 603}, headers=bearer(s)).status_code == 409
    assert vclient.post("/v1/video/requests", json={"kind": "album", "tmdbId": 1396}, headers=bearer(s)).status_code == 400
    fake.on("GET", f"{TMDB}/movie/604", {"id": 604, "title": "The Matrix Reloaded"})
    fake.on("GET", f"{RADARR}/movie", status=500, json_body={})
    rid = vclient.post("/v1/video/requests", json={"kind": "movie", "tmdbId": 604}, headers=bearer(s)).json()["request"]["id"]
    run(requests.start(rid))
    assert db.get(rid)["status"] == "failed" and "Radarr" in db.get(rid)["error"]


def test_video_needs_sign_in_and_only_owners_delete(vclient, fake):
    assert vclient.get("/v1/video/search", params={"q": "x"}).status_code == 401
    from continuo.video import db
    make_user()
    make_user(username="caleb", role="user")
    owner = login(vclient)
    other = login(vclient, username="caleb")
    rid = db.create(kind="movie", tmdb_id=1, title="X", year=None, poster_path=None, user_id=1)["id"]
    assert vclient.delete(f"/v1/video/requests/{rid}", headers=bearer(other)).status_code == 404
    assert vclient.delete(f"/v1/video/requests/{rid}", headers=bearer(owner)).status_code == 204
    assert db.get(rid) is None


def test_video_is_absent_without_the_part(client):
    make_user()
    s = login(client)
    assert client.get("/v1/video/search", params={"q": "x"}, headers=bearer(s)).status_code == 404


def test_discover_marks_trending_and_popular_titles(vclient, fake):
    from continuo.video import db, index
    _jellyfin(fake, [{"Id": "jf-1", "Name": "The Matrix", "Type": "Movie", "ProviderIds": {"Tmdb": "603"}}])
    run(index.refresh())
    db.create(kind="series", tmdb_id=1396, title="Breaking Bad", year=2008, poster_path=None, user_id=None)
    fake.on("GET", f"{TMDB}/trending/all/week", {"results": [
        {"id": 603, "media_type": "movie", "title": "The Matrix"}, {"id": 1396, "media_type": "tv", "name": "Breaking Bad"},
        {"id": 31, "media_type": "person", "name": "Tom Hanks"}]})
    fake.on("GET", f"{TMDB}/movie/popular", {"results": [{"id": 27205, "title": "Inception", "release_date": "2010-07-15"}]})
    fake.on("GET", f"{TMDB}/tv/popular", handler=lambda r: httpx.Response(500))
    make_user()
    s = login(vclient)
    d = vclient.get("/v1/video/discover", headers=bearer(s)).json()
    assert [(h["tmdbId"], h["availability"]["state"]) for h in d["trending"]] == [(603, "in_library"), (1396, "requested")]
    assert d["popularFilms"][0]["kind"] == "movie" and d["popularFilms"][0]["year"] == 2010
    assert d["popularSeries"] == [] and d["catalog"] == "ok", "one shelf failing leaves it empty"
