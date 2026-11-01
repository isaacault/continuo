"""Browsing like Netflix (continuo/video/browse.py) and the richer title pages: cast, age ratings,
More Like This, where this account left off, My List, ratings, watched/unwatched."""
import os

import httpx
import pytest
from conftest import bearer, login, make_user, run

from continuo import config

JELLYFIN = "http://jellyfin.test"
TMDB = "https://api.themoviedb.org/3"
MATRIX = {"id": 603, "title": "The Matrix", "release_date": "1999-03-31", "poster_path": "/m.jpg",
          "backdrop_path": "/b.jpg", "runtime": 136, "overview": "Neo.", "tagline": "Welcome to the Real World.",
          "vote_average": 8.2, "genres": [{"id": 28, "name": "Action"}],
          "videos": {"results": [{"site": "YouTube", "type": "Teaser", "key": "tease"},
                                 {"site": "YouTube", "type": "Trailer", "key": "vKQi3bBA1y8"}]},
          "credits": {"cast": [{"name": "Keanu Reeves", "character": "Neo", "profile_path": "/k.jpg"}],
                      "crew": [{"name": "Lana Wachowski", "job": "Director", "department": "Directing"},
                               {"name": "Lana Wachowski", "job": "Writer", "department": "Writing"}]},
          "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "R"}]},
                                        {"iso_3166_1": "GB", "release_dates": [{"certification": ""}, {"certification": "15"}]}]},
          "images": {"logos": [{"file_path": "/logo.png"}]},
          "recommendations": {"results": [{"id": 604, "title": "The Matrix Reloaded", "release_date": "2003-05-15",
                                           "backdrop_path": "/r.jpg", "genre_ids": [28], "vote_average": 7}]}}
BB = {"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20", "last_air_date": "2013-09-29",
      "number_of_seasons": 5, "episode_run_time": [47], "created_by": [{"name": "Vince Gilligan"}],
      "seasons": [{"season_number": 1, "episode_count": 7}], "genres": [{"id": 18, "name": "Drama"}],
      "content_ratings": {"results": [{"iso_3166_1": "GB", "rating": "18"}]},
      "credits": {"cast": [], "crew": []}, "images": {"logos": []}, "recommendations": {"results": []}, "videos": {"results": []}}


@pytest.fixture
def vclient(fake):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf",
                       TMDB_API_KEY="tm", TMDB_REGION="GB"))
    with TestClient(create_app(disable_workers=True)) as c:
        yield c


def _link(user_id: int = 1):
    from continuo.db.conn import connect, now
    with connect() as c:
        c.execute("INSERT INTO video_accounts (user_id, jellyfin_user_id, jellyfin_username, jellyfin_password, managed,"
                  " created_at) VALUES (?, 'jf-u1', 'isaac', 'pw', 1, ?)", (user_id, now()))


def _library(fake, items):
    from continuo.video import index
    fake.on("GET", f"{JELLYFIN}/Items", {"Items": items})
    run(index.refresh())


def test_a_film_page_has_what_a_detail_screen_shows(vclient, fake):
    _library(fake, [{"Id": "jf-603", "Name": "The Matrix", "Type": "Movie", "ProviderIds": {"Tmdb": "603"}}])
    fake.on("GET", f"{TMDB}/movie/603", MATRIX)
    fake.on("GET", f"{JELLYFIN}/Items/jf-603", {"Id": "jf-603", "Type": "Movie", "Name": "The Matrix",
                                                "UserData": {"PlaybackPositionTicks": 12_000_000_000, "Played": False}})
    make_user()
    _link()
    s = login(vclient)
    m = vclient.get("/v1/video/movies/603", headers=bearer(s)).json()
    assert (m["tagline"], m["voteAverage"], m["certification"]) == ("Welcome to the Real World.", 8.2, "15"), "GB's rating first"
    assert m["cast"] == [{"name": "Keanu Reeves", "character": "Neo", "photo": m["cast"][0]["photo"]}] and "w185" in m["cast"][0]["photo"]
    assert m["directors"] == ["Lana Wachowski"] and m["writers"] == ["Lana Wachowski"]
    assert m["trailerYouTubeKey"] == "vKQi3bBA1y8", "a trailer before a teaser"
    assert "logo.png" in m["logo"] and m["genreList"] == [{"id": 28, "name": "Action"}]
    assert [(h["tmdbId"], h["availability"]["state"]) for h in m["moreLikeThis"]] == [(604, "requestable")]
    assert (m["positionSeconds"], m["played"], m["inMyList"], m["myRating"]) == (1200, False, False, 0)
    assert fake.called("GET", f"{JELLYFIN}/Items/jf-603")[0].url.params["userId"] == "jf-u1"
    params = fake.called("GET", f"{TMDB}/movie/603")[0].url.params
    assert "credits" in params["append_to_response"] and params["include_image_language"] == "en,null"


def test_a_series_page_names_its_makers_and_what_play_plays(vclient, fake):
    _library(fake, [{"Id": "jf-bb", "Name": "Breaking Bad", "Type": "Series", "ProviderIds": {"Tmdb": "1396"}}])
    fake.on("GET", f"{TMDB}/tv/1396", BB)
    fake.on("GET", f"{JELLYFIN}/Shows/jf-bb/Episodes", {"Items": []})
    fake.on("GET", f"{JELLYFIN}/Shows/NextUp", {"Items": [{"Id": "e3", "Type": "Episode", "Name": "...And the Bag's in the River",
                                                          "SeriesId": "jf-bb", "ParentIndexNumber": 1, "IndexNumber": 3}]})
    make_user()
    _link()
    s = login(vclient)
    b = vclient.get("/v1/video/series/1396", headers=bearer(s)).json()
    assert (b["creators"], b["certification"], b["seasonCount"], b["lastYear"]) == (["Vince Gilligan"], "18", 5, 2013)
    assert (b["upNext"]["jellyfinId"], b["upNext"]["episode"]) == ("e3", 3)
    assert fake.called("GET", f"{JELLYFIN}/Shows/NextUp")[0].url.params["SeriesId"] == "jf-bb"


def test_genres_rows_top_10_and_new_and_popular(vclient, fake):
    fake.on("GET", f"{TMDB}/genre/movie/list", {"genres": [{"id": 28, "name": "Action"}, {"id": 35, "name": "Comedy"}]})
    fake.on("GET", f"{TMDB}/discover/tv", {"page": 2, "total_pages": 9, "results": [{"id": 1, "name": "A", "first_air_date": "2020-01-01"}]})
    fake.on("GET", f"{TMDB}/trending/movie/day", {"results": [{"id": n, "title": f"F{n}"} for n in range(1, 13)]})
    fake.on("GET", f"{TMDB}/movie/upcoming", {"results": [{"id": 7, "title": "Later", "release_date": "2026-12-20"},
                                                          {"id": 8, "title": "Sooner", "release_date": "2026-11-20"}]})
    fake.on("GET", f"{TMDB}/tv/on_the_air", {"results": [{"id": 9, "name": "Weekly"}]})
    fake.on("GET", f"{TMDB}/trending/all/week", handler=lambda r: httpx.Response(500))
    make_user()
    s = login(vclient)
    assert vclient.get("/v1/video/genres", params={"kind": "movie"}, headers=bearer(s)).json()["genres"][1] == {"id": 35, "name": "Comedy"}
    b = vclient.get("/v1/video/browse", params={"kind": "series", "genre": 18, "sort": "top_rated", "page": 2}, headers=bearer(s)).json()
    assert (b["page"], b["totalPages"], b["results"][0]["kind"]) == (2, 9, "series")
    p = fake.called("GET", f"{TMDB}/discover/tv")[0].url.params
    assert (p["with_genres"], p["sort_by"], p["vote_count.gte"]) == ("18", "vote_average.desc", "300")
    t = vclient.get("/v1/video/top10", params={"kind": "movie"}, headers=bearer(s)).json()["results"]
    assert [(h["rank"], h["title"]) for h in t][:2] == [(1, "F1"), (2, "F2")] and len(t) == 10
    n = vclient.get("/v1/video/new", headers=bearer(s)).json()
    assert [h["title"] for h in n["comingSoon"]] == ["Sooner", "Later"], "soonest first"
    assert [h["title"] for h in n["newEpisodes"]] == ["Weekly"] and n["trending"] == [], "a part TMDB can't give is empty"
    assert vclient.get("/v1/video/browse", params={"kind": "other"}, headers=bearer(s)).status_code == 422


def test_my_list_and_ratings(vclient, fake):
    fake.on("GET", f"{TMDB}/movie/603", MATRIX)
    fake.on("GET", f"{TMDB}/tv/1396", BB)
    make_user()
    s = login(vclient)
    assert vclient.put("/v1/video/my-list/movie/603", headers=bearer(s)).json() == {"inMyList": True}
    assert vclient.put("/v1/video/my-list/series/1396", headers=bearer(s)).status_code == 200
    assert vclient.put("/v1/video/my-list/movie/603", headers=bearer(s)).status_code == 200, "adding twice is fine"
    out = vclient.get("/v1/video/my-list", headers=bearer(s)).json()["results"]
    assert [(e["kind"], e["title"], e["year"]) for e in out] == [("series", "Breaking Bad", 2008), ("movie", "The Matrix", 1999)]
    assert vclient.get("/v1/video/movies/603", headers=bearer(s)).json()["inMyList"] is True
    assert vclient.delete("/v1/video/my-list/movie/603", headers=bearer(s)).json() == {"inMyList": False}
    assert [e["tmdbId"] for e in vclient.get("/v1/video/my-list", headers=bearer(s)).json()["results"]] == [1396]
    # ratings: thumbs, and the row they start
    assert vclient.put("/v1/video/ratings/movie/603", json={"rating": 2}, headers=bearer(s)).json() == {"myRating": 2}
    assert vclient.put("/v1/video/ratings/movie/603", json={"rating": 5}, headers=bearer(s)).status_code == 400
    assert vclient.get("/v1/video/movies/603", headers=bearer(s)).json()["myRating"] == 2
    rows = vclient.get("/v1/video/because-you-liked", headers=bearer(s)).json()["rows"]
    assert rows[0]["because"]["title"] == "The Matrix" and rows[0]["results"][0]["title"] == "The Matrix Reloaded"
    vclient.put("/v1/video/ratings/movie/603", json={"rating": 0}, headers=bearer(s))
    assert vclient.get("/v1/video/because-you-liked", headers=bearer(s)).json()["rows"] == []
    # each account's own
    make_user(username="caleb", role="user")
    c = login(vclient, username="caleb")
    assert vclient.get("/v1/video/my-list", headers=bearer(c)).json()["results"] == []


def test_watched_and_not_watched_go_to_jellyfin(vclient, fake):
    fake.on("POST", f"{JELLYFIN}/UserPlayedItems/e3", {"Played": True})
    fake.on("DELETE", f"{JELLYFIN}/UserPlayedItems/e3", {"Played": False})
    make_user()
    s = login(vclient)
    assert vclient.put("/v1/video/played/e3", json={"played": True}, headers=bearer(s)).status_code == 409, "no Jellyfin user yet"
    _link()
    assert vclient.put("/v1/video/played/e3", json={"played": True}, headers=bearer(s)).json() == {"played": True}
    assert vclient.put("/v1/video/played/e3", json={"played": False}, headers=bearer(s)).json() == {"played": False}
    assert fake.called("POST", f"{JELLYFIN}/UserPlayedItems/e3")[0].url.params["userId"] == "jf-u1"
    assert len(fake.called("DELETE", f"{JELLYFIN}/UserPlayedItems/e3")) == 1
    assert vclient.put("/v1/video/played/..%2Fx", json={"played": True}, headers=bearer(s)).status_code in (404, 422)


def test_home_episodes_name_their_series(vclient, fake):
    _library(fake, [{"Id": "jf-bb", "Name": "Breaking Bad", "Type": "Series", "ProviderIds": {"Tmdb": "1396"}}])
    fake.on("GET", f"{JELLYFIN}/UserItems/Resume", {"Items": [{"Id": "e1", "Type": "Episode", "Name": "Pilot", "SeriesId": "jf-bb",
                                                              "SeriesName": "Breaking Bad", "ParentIndexNumber": 1, "IndexNumber": 1}]})
    fake.on("GET", f"{JELLYFIN}/Shows/NextUp", {"Items": []})
    make_user()
    _link()
    s = login(vclient)
    h = vclient.get("/v1/video/home", headers=bearer(s)).json()
    assert h["continueWatching"][0]["seriesTmdbId"] == 1396
    assert fake.called("GET", f"{JELLYFIN}/UserItems/Resume")[0].url.params["userId"] == "jf-u1"



def test_settings_say_how_the_apps_look(vclient, fake):
    make_user()
    s = login(vclient)
    assert vclient.get("/v1/video/settings", headers=bearer(s)).json() == {"ui": "library", "animeLibraries": ["Anime"], "canRemove": True}
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", VIDEO_UI="netflix", VIDEO_ANIME_LIBRARIES="Anime, Anime films"))
    assert vclient.get("/v1/video/settings", headers=bearer(s)).json() == {"ui": "netflix", "animeLibraries": ["Anime", "Anime films"], "canRemove": True}
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", VIDEO_UI="something-else"))
    assert vclient.get("/v1/video/settings", headers=bearer(s)).json()["ui"] == "library", "anything else: the default"


def test_explore_filters_anime_years_rating_language(vclient, fake):
    fake.on("GET", f"{TMDB}/discover/tv", {"page": 1, "total_pages": 1, "results": []})
    make_user()
    s = login(vclient)
    r = vclient.get("/v1/video/browse", params={"kind": "series", "genre": 10765, "anime": "true", "yearFrom": 2010, "yearTo": 2019,
                                                "minRating": 7.5, "language": "ja", "sort": "new"}, headers=bearer(s))
    assert r.status_code == 200, r.text
    p = fake.called("GET", f"{TMDB}/discover/tv")[0].url.params
    assert (p["with_genres"], p["with_origin_country"], p["with_original_language"]) == ("10765,16", "JP", "ja")
    assert (p["first_air_date.gte"], p["first_air_date.lte"]) == ("2010-01-01", "2019-12-31"), "a range in the past stays in the past"
    assert (p["vote_average.gte"], p["vote_count.gte"]) == ("7.5", "50")
    assert vclient.get("/v1/video/browse", params={"kind": "movie", "language": "japanese"}, headers=bearer(s)).status_code == 422



RADARR = "http://radarr.test/api/v3"
SONARR = "http://sonarr.test/api/v3"


def test_removing_from_the_library(vclient, fake):
    from continuo.db import users
    from continuo.video import index
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf", TMDB_API_KEY="tm",
                       RADARR_URL="http://radarr.test", RADARR_API_KEY="rd", SONARR_URL="http://sonarr.test", SONARR_API_KEY="so"))
    _library(fake, [{"Id": "jf-603", "Name": "The Matrix", "Type": "Movie", "ProviderIds": {"Tmdb": "603"}},
                    {"Id": "jf-bb", "Name": "Breaking Bad", "Type": "Series", "ProviderIds": {"Tmdb": "1396"}},
                    {"Id": "jf-home", "Name": "Home video", "Type": "Movie", "ProviderIds": {"Tmdb": "999"}}])
    make_user(role="user")
    s = login(vclient)
    assert vclient.get("/v1/video/settings", headers=bearer(s)).json()["canRemove"] is False
    assert vclient.delete("/v1/video/library/movie/603", headers=bearer(s)).status_code == 403, "admins only, as music's"
    users.set_role(users.get("isaac")["id"], "admin")
    s = login(vclient)
    assert vclient.get("/v1/video/settings", headers=bearer(s)).json()["canRemove"] is True
    # a film Radarr has: through Radarr, files and all
    fake.on("GET", f"{RADARR}/movie", [{"id": 7, "tmdbId": 603}])
    fake.on("DELETE", f"{RADARR}/movie/7", {})
    assert vclient.delete("/v1/video/library/movie/603", headers=bearer(s)).json() == {"removed": True, "via": "radarr"}
    assert fake.called("DELETE", f"{RADARR}/movie/7")[0].url.params["deleteFiles"] == "true"
    assert index.by_tmdb("movie", 603) is None
    # a series Sonarr has
    fake.on("GET", f"{TMDB}/tv/1396", BB | {"external_ids": {"tvdb_id": 81189}})
    fake.on("GET", f"{SONARR}/series", [{"id": 5, "tvdbId": 81189}])
    fake.on("DELETE", f"{SONARR}/series/5", {})
    assert vclient.delete("/v1/video/library/series/1396", headers=bearer(s)).json()["via"] == "sonarr"
    assert fake.called("DELETE", f"{SONARR}/series/5")[0].url.params["deleteFiles"] == "true"
    # a film nobody manages: Jellyfin deletes it
    fake.on("GET", f"{RADARR}/movie", [])
    fake.on("DELETE", f"{JELLYFIN}/Items/jf-home", handler=lambda r: httpx.Response(204))
    assert vclient.delete("/v1/video/library/movie/999", headers=bearer(s)).json()["via"] == "jellyfin"
    assert vclient.delete("/v1/video/library/movie/12345", headers=bearer(s)).status_code == 404
