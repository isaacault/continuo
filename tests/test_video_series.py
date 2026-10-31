"""Series: pages with partial seasons, season pages with watch state, requests through Sonarr."""
import os

import httpx
import pytest
from conftest import bearer, login, make_user, run

from continuo import config

JELLYFIN = "http://jellyfin.test"
SONARR = "http://sonarr.test/api/v3"
TMDB = "https://api.themoviedb.org/3"
BB = {"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20", "status": "Ended", "genres": [{"name": "Drama"}],
      "external_ids": {"tvdb_id": 81189},
      "seasons": [{"season_number": 0, "episode_count": 9, "name": "Specials"},
                  {"season_number": 1, "episode_count": 7, "name": "Season 1"},
                  {"season_number": 2, "episode_count": 13, "name": "Season 2"}]}


@pytest.fixture
def vclient(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf",
                       SONARR_URL="http://sonarr.test", SONARR_API_KEY="so", TMDB_API_KEY="tm",
                       SONARR_ROOT_FOLDER="/tv", SONARR_QUALITY_PROFILE_ID="6"))
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def _in_library(fake):
    from continuo.video import index
    fake.on("GET", f"{JELLYFIN}/Items", {"Items": [{"Id": "jf-bb", "Name": "Breaking Bad", "Type": "Series",
                                                    "ProviderIds": {"Tmdb": "1396", "Tvdb": "81189"}}]})
    run(index.refresh())


def test_a_series_page_counts_each_season_against_tmdb(vclient, fake):
    _in_library(fake)
    fake.on("GET", f"{TMDB}/tv/1396", BB)
    eps = [{"Id": f"e1-{i}", "ParentIndexNumber": 1, "IndexNumber": i} for i in range(1, 8)] + \
          [{"Id": f"e2-{i}", "ParentIndexNumber": 2, "IndexNumber": i} for i in range(1, 5)]
    fake.on("GET", f"{JELLYFIN}/Shows/jf-bb/Episodes", {"Items": eps})
    make_user()
    s = login(vclient)
    page = vclient.get("/v1/video/series/1396", headers=bearer(s)).json()
    assert page["title"] == "Breaking Bad" and page["year"] == 2008 and page["availability"]["state"] == "in_library"
    assert [(x["number"], x["have"], x["episodes"], x["state"]) for x in page["seasons"]] == \
        [(0, 0, 9, "missing"), (1, 7, 7, "in_library"), (2, 4, 13, "partial")]


def test_a_season_page_shows_watch_state_for_the_accounts_jellyfin_user(vclient, fake):
    from continuo.db.conn import connect, now
    _in_library(fake)
    fake.on("GET", f"{TMDB}/tv/1396/season/1", {"name": "Season 1", "episodes": [
        {"episode_number": 1, "name": "Pilot", "runtime": 58}, {"episode_number": 2, "name": "Cat's in the Bag..."}]})
    fake.on("GET", f"{JELLYFIN}/Shows/jf-bb/Episodes", {"Items": [
        {"Id": "e1-1", "ParentIndexNumber": 1, "IndexNumber": 1, "UserData": {"Played": True}},
        {"Id": "e1-2", "ParentIndexNumber": 1, "IndexNumber": 2, "UserData": {"PlaybackPositionTicks": 6_000_000_000}}]})
    make_user()
    with connect() as c:
        c.execute("INSERT INTO video_accounts (user_id, jellyfin_user_id, jellyfin_username, jellyfin_password, managed,"
                  " created_at) VALUES (1, 'jf-u1', 'isaac', 'pw', 1, ?)", (now(),))
    s = login(vclient)
    page = vclient.get("/v1/video/series/1396/seasons/1", headers=bearer(s)).json()
    assert [(e["number"], e["inLibrary"], e["played"], e["positionSeconds"]) for e in page["episodes"]] == [(1, True, True, 0), (2, True, False, 600)]
    assert fake.called("GET", f"{JELLYFIN}/Shows/jf-bb/Episodes")[0].url.params["UserId"] == "jf-u1"


def test_a_series_request_adds_only_the_asked_seasons_and_ends_available(vclient, fake):
    from continuo.video import db, requests
    fake.on("GET", f"{TMDB}/tv/1396", BB)
    fake.on("GET", f"{SONARR}/series", [])
    fake.on("GET", f"{SONARR}/series/lookup", [{"tvdbId": 81189, "title": "Breaking Bad", "seasons": [
        {"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2}]}])
    fake.on("POST", f"{SONARR}/series", handler=lambda r: httpx.Response(201, json={"id": 5, **fake.body(r)}))
    make_user()
    s = login(vclient)
    r = vclient.post("/v1/video/requests", json={"kind": "series", "tmdbId": 1396, "seasons": [2]}, headers=bearer(s))
    assert r.status_code == 201, r.text
    rid = r.json()["request"]["id"]
    assert r.json()["request"]["seasons"] == [2]
    run(requests.start(rid))
    body = fake.body(fake.called("POST", f"{SONARR}/series")[0])
    assert [(x["seasonNumber"], x["monitored"]) for x in body["seasons"]] == [(0, False), (1, False), (2, True)]
    assert body["monitorNewItems"] == "none" and body["addOptions"] == {"searchForMissingEpisodes": True}
    assert body["qualityProfileId"] == 6 and body["rootFolderPath"] == "/tv"
    assert db.get(rid)["status"] == "searching" and db.get(rid)["sonarr_series_id"] == 5
    # complete in Sonarr, in Jellyfin: available
    fake.on("GET", f"{SONARR}/series/5", {"id": 5, "seasons": [
        {"seasonNumber": 1, "statistics": {"episodeCount": 7, "episodeFileCount": 0}},
        {"seasonNumber": 2, "statistics": {"episodeCount": 13, "episodeFileCount": 13}}]})
    fake.on("GET", f"{SONARR}/queue/details", [])
    _in_library(fake)
    run(requests.tick())
    assert db.get(rid)["status"] == "available" and db.get(rid)["jellyfin_id"] == "jf-bb"


def test_a_series_already_in_sonarr_gets_the_asked_seasons_monitored_and_searched(vclient, fake):
    from continuo.video.upstream import sonarr
    fake.on("GET", f"{SONARR}/series", [{"id": 5, "tvdbId": 81189, "seasons": [
        {"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": False}]}])
    fake.on("PUT", f"{SONARR}/series/5", handler=lambda r: fake.body(r))
    fake.on("POST", f"{SONARR}/command", {"id": 1})
    run(sonarr.request(81189, [2]))
    put = fake.body(fake.called("PUT", f"{SONARR}/series/5")[0])
    assert [(x["seasonNumber"], x["monitored"]) for x in put["seasons"]] == [(1, True), (2, True)]
    assert [fake.body(c) for c in fake.called("POST", f"{SONARR}/command")] == [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 2}]


def test_a_series_tmdb_cant_map_to_tvdb_fails_with_a_reason(vclient, fake):
    from continuo.video import db, requests
    fake.on("GET", f"{TMDB}/tv/99", {"id": 99, "name": "Obscure", "external_ids": {}})
    make_user()
    s = login(vclient)
    rid = vclient.post("/v1/video/requests", json={"kind": "series", "tmdbId": 99}, headers=bearer(s)).json()["request"]["id"]
    run(requests.start(rid))
    assert db.get(rid)["status"] == "failed" and "TVDB" in db.get(rid)["error"]
