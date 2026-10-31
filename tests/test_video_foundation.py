"""The video part's foundation (docs/design/video.md §11 step 4): its own tables, the Jellyfin index,
TMDB and Radarr clients, health checks; only when CONTINUO_MODULES lists "video"."""
import os
import sqlite3
import subprocess
import sys

import httpx
from conftest import run

from continuo import config, modules, workers
from continuo.db import migrate

JELLYFIN = "http://jellyfin.test"
RADARR = "http://radarr.test/api/v3"
TMDB = "https://api.themoviedb.org/3"


def _video(extra: dict | None = None):
    env = dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL=JELLYFIN, JELLYFIN_API_KEY="jf-key",
               RADARR_URL="http://radarr.test", RADARR_API_KEY="rd-key", TMDB_API_KEY="tmdb-key",
               RADARR_ROOT_FOLDER="/movies", RADARR_QUALITY_PROFILE_ID="4")
    config.reload({**env, **(extra or {})})
    migrate.run()


def _tables() -> set[str]:
    with sqlite3.connect(config.settings.db_path) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_video_tables_exist_only_with_the_video_part(fake):
    migrate.run()
    assert "video_library" not in _tables(), "music alone: no video tables"
    _video()
    assert {"video_library", "video_requests", "video_accounts", "video_tmdb_cache"} <= _tables()


def test_video_brings_its_loop_and_health_checks(fake):
    _video()
    assert "video_index" in workers.loop_specs(modules.load())
    video = next(p for p in modules.load() if p.__name__ == "continuo.video")
    checks = video.health_checks()
    assert set(checks) == {"jellyfin", "radarr", "sonarr", "tmdb"}
    for c in checks.values():
        c.close()


def test_the_index_reads_jellyfin_and_stops_when_paging_is_ignored(fake):
    _video()
    from continuo.video import index
    from continuo.video.upstream import jellyfin
    jellyfin.PAGE = 2
    items = [{"Id": "jf-1", "Name": "The Matrix", "Type": "Movie", "ProductionYear": 1999,
              "ProviderIds": {"Tmdb": "603", "Imdb": "tt0133093"}},
             {"Id": "jf-2", "Name": "Breaking Bad", "Type": "Series", "ProductionYear": 2008,
              "ProviderIds": {"Tmdb": "1396", "Tvdb": "81189"}}]
    fake.on("GET", f"{JELLYFIN}/Items", handler=lambda r: {"Items": items, "TotalRecordCount": 2})   # every page the same
    try:
        assert run(index.refresh()) == {"items": 2, "removed": 0}
    finally:
        jellyfin.PAGE = 200
    assert len(fake.called("GET", f"{JELLYFIN}/Items")) == 2, "the repeat page ended it"
    assert fake.called("GET", f"{JELLYFIN}/Items")[0].headers["X-Emby-Token"] == "jf-key"
    assert index.by_tmdb("movie", 603)["jellyfin_id"] == "jf-1"
    assert index.by_tmdb("series", 1396)["tvdb_id"] == 81189
    fake.on("GET", f"{JELLYFIN}/Items", {"Items": items[:1]})
    assert run(index.refresh()) == {"items": 1, "removed": 1}


def test_tmdb_search_keeps_films_and_series_and_caches(fake):
    _video()
    from continuo.video.upstream import tmdb
    fake.on("GET", f"{TMDB}/search/multi", {"results": [
        {"id": 603, "media_type": "movie", "title": "The Matrix", "release_date": "1999-03-31", "poster_path": "/m.jpg"},
        {"id": 1396, "media_type": "tv", "name": "Breaking Bad", "first_air_date": "2008-01-20"},
        {"id": 6384, "media_type": "person", "name": "Keanu Reeves"}]})
    hits = run(tmdb.search("matrix"))
    assert [(h["kind"], h["tmdbId"], h["year"]) for h in hits] == [("movie", 603, 1999), ("series", 1396, 2008)]
    req = fake.called("GET", f"{TMDB}/search/multi")[0]
    assert req.url.params["api_key"] == "tmdb-key" and req.url.params["language"] == "en-GB"
    run(tmdb.search("matrix"))
    assert len(fake.called("GET", f"{TMDB}/search/multi")) == 1, "the second answer came from the cache"


def test_tmdb_v4_token_goes_as_bearer(fake):
    _video({"TMDB_API_KEY": "eyJtoken"})
    from continuo.upstream import http
    from continuo.video.upstream import tmdb
    http._clients.clear()
    fake.on("GET", f"{TMDB}/movie/603", {"id": 603, "title": "The Matrix"})
    run(tmdb.movie(603))
    req = fake.called("GET", f"{TMDB}/movie/603")[0]
    assert req.headers["Authorization"] == "Bearer eyJtoken" and "api_key" not in req.url.params


def test_radarr_adds_a_film_monitored_and_searching_and_keeps_one_already_there(fake):
    _video()
    from continuo.video.upstream import radarr
    fake.on("GET", f"{RADARR}/movie", [])
    fake.on("GET", f"{RADARR}/movie/lookup/tmdb", {"tmdbId": 603, "title": "The Matrix", "year": 1999})
    fake.on("POST", f"{RADARR}/movie", handler=lambda r: httpx.Response(201, json={"id": 9, **fake.body(r)}))
    added = run(radarr.add(603))
    body = fake.body(fake.called("POST", f"{RADARR}/movie")[0])
    assert added["id"] == 9 and body["monitored"] is True and body["addOptions"] == {"searchForMovie": True}
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/movies"
    fake.on("GET", f"{RADARR}/movie", [{"id": 9, "tmdbId": 603}])
    assert run(radarr.add(603))["id"] == 9 and len(fake.called("POST", f"{RADARR}/movie")) == 1


def test_video_alone_loads_no_music_code(fake):
    code = ("import sys; from continuo.main import create_app; create_app(disable_workers=True); "
            "print(' '.join(sorted(m for m in sys.modules if m.startswith('continuo.'))))")
    loaded = set(subprocess.run([sys.executable, "-c", code], env=dict(os.environ, CONTINUO_MODULES="video"),
                                capture_output=True, text=True, check=True).stdout.split())
    assert "continuo.video" in loaded
    assert not loaded & {"continuo.music", "continuo.upstream.lidarr", "continuo.upstream.navidrome",
                         "continuo.upstream.slskd", "continuo.workers.index", "continuo.search"}
