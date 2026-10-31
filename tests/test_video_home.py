"""The video home: Jellyfin's shelves for the account's Jellyfin user, and requests on their way."""
import os

import httpx
import pytest
from conftest import bearer, login, make_user

from continuo import config

JELLYFIN = "http://jellyfin.test"


@pytest.fixture
def vclient(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf"))
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def _link(user_id=1):
    from continuo.db.conn import connect, now
    with connect() as c:
        c.execute("INSERT INTO video_accounts (user_id, jellyfin_user_id, jellyfin_username, jellyfin_password, managed,"
                  " created_at) VALUES (?, 'jf-u1', 'isaac', 'pw', 1, ?)", (user_id, now()))


def test_home_has_jellyfins_shelves_and_the_requests_on_their_way(vclient, fake):
    from continuo.video import db
    fake.on("GET", f"{JELLYFIN}/Users/jf-u1/Items/Resume", {"Items": [
        {"Id": "e1", "Type": "Episode", "Name": "Pilot", "SeriesName": "Breaking Bad", "SeriesId": "jf-bb",
         "ParentIndexNumber": 1, "IndexNumber": 1, "RunTimeTicks": 34_800_000_000, "UserData": {"PlaybackPositionTicks": 6_000_000_000}}]})
    fake.on("GET", f"{JELLYFIN}/Shows/NextUp", {"Items": [{"Id": "e2", "Type": "Episode", "Name": "Cat's in the Bag...",
                                                          "SeriesName": "Breaking Bad", "ParentIndexNumber": 1, "IndexNumber": 2}]})
    fake.on("GET", f"{JELLYFIN}/Users/jf-u1/Items/Latest", [{"Id": "jf-1", "Type": "Movie", "Name": "The Matrix",
                                                             "ProductionYear": 1999, "ProviderIds": {"Tmdb": "603"}}])
    make_user()
    _link()
    db.create(kind="movie", tmdb_id=604, title="The Matrix Reloaded", year=2003, poster_path=None, user_id=1)
    s = login(vclient)
    h = vclient.get("/v1/video/home", headers=bearer(s)).json()
    cw = h["continueWatching"][0]
    assert (cw["kind"], cw["seriesTitle"], cw["season"], cw["episode"], cw["positionSeconds"], cw["runtimeSeconds"]) == \
        ("episode", "Breaking Bad", 1, 1, 600, 3480)
    assert h["nextUp"][0]["episode"] == 2 and h["recentlyAdded"][0]["tmdbId"] == 603
    assert [r["title"] for r in h["requests"]] == ["The Matrix Reloaded"] and h["linked"] is True
    assert fake.called("GET", f"{JELLYFIN}/Shows/NextUp")[0].url.params["UserId"] == "jf-u1"


def test_home_without_a_jellyfin_user_or_with_jellyfin_down(vclient, fake):
    make_user()
    s = login(vclient)
    h = vclient.get("/v1/video/home", headers=bearer(s)).json()
    assert h == {"continueWatching": [], "nextUp": [], "recentlyAdded": [], "requests": [], "linked": False}
    _link()
    fake.on("GET", f"{JELLYFIN}/Users/jf-u1/Items/Resume", handler=lambda r: httpx.Response(500))
    fake.on("GET", f"{JELLYFIN}/Shows/NextUp", {"Items": []})
    fake.on("GET", f"{JELLYFIN}/Users/jf-u1/Items/Latest", [])
    assert vclient.get("/v1/video/home", headers=bearer(s)).json()["continueWatching"] == [], "a shelf that fails is empty"
