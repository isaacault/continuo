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
    # Future episodes too: new ones arrive as they air (the user's choice).
    assert body["monitorNewItems"] == "all" and body["addOptions"] == {"searchForMissingEpisodes": True}
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
    assert put["monitorNewItems"] == "all", "a series already there gets future episodes too"
    assert [fake.body(c) for c in fake.called("POST", f"{SONARR}/command")] == [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 2}]


def test_a_series_tmdb_cant_map_to_tvdb_fails_with_a_reason(vclient, fake):
    from continuo.video import db, requests
    fake.on("GET", f"{TMDB}/tv/99", {"id": 99, "name": "Obscure", "external_ids": {}})
    make_user()
    s = login(vclient)
    rid = vclient.post("/v1/video/requests", json={"kind": "series", "tmdbId": 99}, headers=bearer(s)).json()["request"]["id"]
    run(requests.start(rid))
    assert db.get(rid)["status"] == "failed" and "TVDB" in db.get(rid)["error"]



def test_anime_goes_to_sonarrs_anime_folder_as_anime(vclient, fake):
    """The homelab's Sonarr keeps anime in /data/anime as its anime type (Jellyfin's Anime library)."""
    from continuo.video import db, requests
    from continuo.video.upstream import tmdb
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", SONARR_URL="http://sonarr.test", SONARR_API_KEY="so",
                       TMDB_API_KEY="tm", SONARR_ROOT_FOLDER="/tv", SONARR_ANIME_ROOT_FOLDER="/anime", SONARR_QUALITY_PROFILE_ID="7"))
    frieren = {"id": 209867, "name": "Frieren", "genres": [{"id": 16}, {"id": 10765}], "origin_country": ["JP"],
               "external_ids": {"tvdb_id": 424536}, "seasons": [{"season_number": 1, "episode_count": 28}]}
    assert tmdb.is_anime(frieren) and not tmdb.is_anime({**frieren, "origin_country": ["US"], "original_language": "en"})
    assert not tmdb.is_anime({**frieren, "genres": [{"id": 18}]}), "Japanese, not animated: a drama"
    fake.on("GET", f"{TMDB}/tv/209867", frieren)
    fake.on("GET", f"{SONARR}/series", [])
    fake.on("GET", f"{SONARR}/series/lookup", [{"tvdbId": 424536, "title": "Frieren", "seasons": [{"seasonNumber": 1}]}])
    fake.on("POST", f"{SONARR}/series", handler=lambda r: httpx.Response(201, json={"id": 9, **fake.body(r)}))
    req = db.create(kind="series", tmdb_id=209867, title="Frieren", year=2023, poster_path=None, user_id=None)
    run(requests.start(req["id"]))
    body = fake.body(fake.called("POST", f"{SONARR}/series")[0])
    assert (body["seriesType"], body["rootFolderPath"], body["qualityProfileId"]) == ("anime", "/anime", 7)


# --- episodes, not only seasons ---------------------------------------------------------------------

RATW = [  # Race Across the World in Sonarr: season 3 done, season 4 two episodes in and still airing
    {"id": 31, "seasonNumber": 3, "episodeNumber": 1, "hasFile": True, "episodeFileId": 301, "airDateUtc": "2023-04-01T20:00:00Z"},
    {"id": 32, "seasonNumber": 3, "episodeNumber": 2, "hasFile": True, "episodeFileId": 301, "airDateUtc": "2023-04-08T20:00:00Z"},
    {"id": 41, "seasonNumber": 4, "episodeNumber": 1, "hasFile": True, "episodeFileId": 401, "airDateUtc": "2026-10-20T20:00:00Z"},
    {"id": 42, "seasonNumber": 4, "episodeNumber": 2, "hasFile": False, "airDateUtc": "2026-10-27T20:00:00Z"},
    {"id": 43, "seasonNumber": 4, "episodeNumber": 3, "hasFile": False, "airDateUtc": "2026-11-03T20:00:00Z"},
    {"id": 44, "seasonNumber": 4, "episodeNumber": 4, "hasFile": False, "airDateUtc": "2099-01-01T20:00:00Z"},
]


def test_a_selection_picks_whole_seasons_and_episodes_with_the_later_ones_for_onward():
    from continuo.video.upstream import sonarr
    onward = [{"season": 4, "episodes": [3, 4], "onward": True}]
    assert sonarr.selected(RATW, [], onward) == {43, 44}
    assert sonarr.selected(RATW, [], [{"season": 4, "episodes": [3], "onward": True}]) == {43, 44}, "and what comes after"
    assert sonarr.selected(RATW, [], [{"season": 4, "episodes": [3], "onward": False}]) == {43}
    assert sonarr.selected(RATW, [3], onward) == {31, 32, 43, 44}
    assert sonarr.selected(RATW, None, None) == {31, 32, 41, 42, 43, 44}, "nothing named: everything"
    # Sonarr's season flag (which monitors the episodes it adds later) is on only for whole seasons and onward picks.
    assert sonarr.season_flags([], onward) == [4] and sonarr.season_flags([3], [{"season": 4, "episodes": [3]}]) == [3]
    assert sonarr.season_flags(None, None) is None


def test_a_series_request_for_episodes_monitors_and_searches_only_those(vclient, fake):
    from continuo.video import db, requests
    fake.on("GET", f"{TMDB}/tv/1396", BB)
    fake.on("GET", f"{SONARR}/series", [])
    fake.on("GET", f"{SONARR}/series/lookup", [{"tvdbId": 81189, "title": "Breaking Bad", "seasons": [
        {"seasonNumber": 3}, {"seasonNumber": 4}]}])
    fake.on("POST", f"{SONARR}/series", handler=lambda r: httpx.Response(201, json={"id": 5, **fake.body(r)}))
    fake.on("GET", f"{SONARR}/episode", [{**e, "hasFile": False} for e in RATW])
    fake.on("PUT", f"{SONARR}/episode/monitor", [])
    fake.on("POST", f"{SONARR}/command", {"id": 1})
    make_user()
    s = login(vclient)
    r = vclient.post("/v1/video/requests", json={"kind": "series", "tmdbId": 1396, "future": False,
                                                 "episodes": [{"season": 4, "episodes": [4, 3, 3], "onward": True}]}, headers=bearer(s))
    assert r.status_code == 201, r.text
    req = r.json()["request"]
    assert req["episodes"] == [{"season": 4, "episodes": [3, 4], "onward": True}] and req["seasons"] == [] and req["future"] is False
    run(requests.start(req["id"]))
    body = fake.body(fake.called("POST", f"{SONARR}/series")[0])
    assert [(x["seasonNumber"], x["monitored"]) for x in body["seasons"]] == [(3, False), (4, True)]
    assert body["addOptions"] == {"searchForMissingEpisodes": False}, "nothing searched before the picks are set"
    assert body["monitorNewItems"] == "none"
    # (The request's own start task ran too, in the test client: the last run is the one checked.)
    monitor = [fake.body(c) for c in fake.called("PUT", f"{SONARR}/episode/monitor")]
    assert monitor[-2:] == [{"episodeIds": [43, 44], "monitored": True}, {"episodeIds": [41, 42], "monitored": False}]
    assert fake.body(fake.called("POST", f"{SONARR}/command")[-1]) == {"name": "EpisodeSearch", "episodeIds": [43]}, \
        "only what's aired"
    assert all(fake.body(c)["name"] == "EpisodeSearch" for c in fake.called("POST", f"{SONARR}/command"))
    assert db.get(req["id"])["status"] == "searching"


def test_a_request_for_episodes_still_to_air_waits_for_them():
    from continuo.video.upstream import sonarr
    show = {"seasons": [{"seasonNumber": 4, "statistics": {"episodeCount": 0, "episodeFileCount": 0}}]}
    assert not sonarr.seasons_complete(show, [4]), "nothing aired yet isn't done"
    show["seasons"][0]["statistics"] = {"episodeCount": 1, "episodeFileCount": 1}
    assert sonarr.seasons_complete(show, [4])


def test_changing_a_request_makes_sonarr_want_exactly_the_new_selection(vclient, fake):
    from continuo.video import db
    show = {"id": 5, "tvdbId": 81189, "monitored": True, "monitorNewItems": "all",
            "seasons": [{"seasonNumber": 3, "monitored": True}, {"seasonNumber": 4, "monitored": True}]}
    fake.on("GET", f"{SONARR}/series/5", show)
    fake.on("PUT", f"{SONARR}/series/5", handler=lambda r: fake.body(r))
    fake.on("GET", f"{SONARR}/episode", RATW)
    fake.on("PUT", f"{SONARR}/episode/monitor", [])
    fake.on("GET", f"{SONARR}/queue/details", [{"id": 900, "episodeId": 42}, {"id": 901, "episodeId": 43}])
    fake.on("DELETE", f"{SONARR}/queue/900", {})
    fake.on("DELETE", f"{SONARR}/episodefile/301", {})
    fake.on("DELETE", f"{SONARR}/episodefile/401", {})
    fake.on("POST", f"{SONARR}/command", {"id": 1})
    uid = make_user()["id"]
    req = db.create(kind="series", tmdb_id=1396, title="Race Across the World", year=2019, poster_path=None, user_id=uid)
    db.update(req["id"], status="downloading", sonarr_series_id=5)
    s = login(vclient)
    r = vclient.put(f"/v1/video/requests/{req['id']}", headers=bearer(s), json={
        "episodes": [{"season": 4, "episodes": [3, 4], "onward": True}], "future": False, "deleteFiles": True})
    assert r.status_code == 200, r.text
    assert r.json()["stoppedDownloads"] == 1 and r.json()["deletedFiles"] == 2
    put = fake.body(fake.called("PUT", f"{SONARR}/series/5")[0])
    assert [(x["seasonNumber"], x["monitored"]) for x in put["seasons"]] == [(3, False), (4, True)] and put["monitorNewItems"] == "none"
    assert [fake.body(c) for c in fake.called("PUT", f"{SONARR}/episode/monitor")] == [
        {"episodeIds": [43, 44], "monitored": True}, {"episodeIds": [31, 32, 41, 42], "monitored": False}]
    assert not fake.called("DELETE", f"{SONARR}/queue/901"), "a wanted episode's download carries on"
    assert len(fake.called("DELETE", f"{SONARR}/episodefile/301")) == 1, "a double episode's file, neither wanted"
    assert [fake.body(c) for c in fake.called("POST", f"{SONARR}/command")] == [{"name": "EpisodeSearch", "episodeIds": [43]}]
    out = r.json()["request"]
    assert out["episodes"] == [{"season": 4, "episodes": [3, 4], "onward": True}] and out["future"] is False
    assert db.get(req["id"])["status"] == "searching"


def test_changing_a_request_keeps_files_unless_asked_and_only_for_its_owner(vclient, fake):
    from continuo.video import db
    fake.on("GET", f"{SONARR}/series/5", {"id": 5, "seasons": [{"seasonNumber": 3}, {"seasonNumber": 4}]})
    fake.on("PUT", f"{SONARR}/series/5", handler=lambda r: fake.body(r))
    fake.on("GET", f"{SONARR}/episode", RATW)
    fake.on("PUT", f"{SONARR}/episode/monitor", [])
    fake.on("GET", f"{SONARR}/queue/details", [])
    fake.on("POST", f"{SONARR}/command", {"id": 1})
    uid = make_user(role="user")["id"]
    other = make_user("someone", role="user")["id"]
    req = db.create(kind="series", tmdb_id=1396, title="Race Across the World", year=2019, poster_path=None, user_id=other)
    db.update(req["id"], status="available", sonarr_series_id=5)
    s = login(vclient)
    url = f"/v1/video/requests/{req['id']}"
    assert vclient.put(url, headers=bearer(s), json={"seasons": [4]}).status_code == 404, "someone else's request"
    db.update(req["id"], user_id=uid)
    assert vclient.put(url, headers=bearer(s), json={"seasons": []}).status_code == 400, "nothing asked"
    r = vclient.put(url, headers=bearer(s), json={"seasons": [4]})
    assert r.status_code == 200 and r.json()["deletedFiles"] == 0
    assert not [c for c in fake.calls if c.method == "DELETE"]
    assert db.get(req["id"])["status"] == "available", "a request that came through stays so (no second notice)"
