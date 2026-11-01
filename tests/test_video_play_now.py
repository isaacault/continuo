"""Play now for films: Radarr's download found in qBittorrent, switched to download in order, and the
film's file served as far as it has arrived (continuo/video/playnow.py)."""
import os

import httpx
import pytest
from conftest import bearer, login, make_user, run

from continuo import config

RADARR = "http://radarr.test/api/v3"
QBT = "http://qbt.test/api/v2"
TMDB = "https://api.themoviedb.org/3"
PIECE = 1024
HASH = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"


@pytest.fixture
def vclient(fake, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    downloads = tmp_path / "downloads"
    (downloads / "incomplete" / "Film.2025").mkdir(parents=True)
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL="http://jellyfin.test", JELLYFIN_API_KEY="jf",
                       RADARR_URL="http://radarr.test", RADARR_API_KEY="rd", QBITTORRENT_URL="http://qbt.test",
                       QBITTORRENT_USERNAME="admin", QBITTORRENT_PASSWORD="pw", VIDEO_DOWNLOADS_DIR=str(downloads),
                       QBITTORRENT_DOWNLOADS_DIR="/data/downloads", PUBLIC_BASE_URL="https://api.example"))
    from continuo.video import playnow
    from continuo.video.upstream import qbittorrent
    playnow._layouts.clear()
    playnow._swapped.clear()
    playnow._mode_checked.clear()
    qbittorrent._flipped.clear()
    monkeypatch.setattr(qbittorrent, "SETTLE_CHECK_SECONDS", 0)
    with TestClient(create_app(disable_workers=True)) as c:
        c.downloads = downloads
        yield c


def _torrent(fake, vclient, size: int, have_pieces: int, *, total_pieces: int, name="Film.2025/Film.2025.mkv", toggles=None):
    """A two-file torrent (a 100-byte NFO first, then the film), [have_pieces] arrived from the start."""
    data = bytes(i % 251 for i in range(size))
    (vclient.downloads / "incomplete" / name).write_bytes(data)
    fake.on("GET", f"{RADARR}/queue/details", [{"movieId": 7, "downloadId": HASH, "protocol": "torrent"}])
    calls = toggles if toggles is not None else []
    fake.on("GET", f"{QBT}/torrents/info", [{"hash": HASH.lower(), "piece_size": PIECE, "progress": 0.1, "dlspeed": 5000,
                                             "num_seeds": 9, "eta": 600, "seq_dl": False, "f_l_piece_prio": True,
                                             "save_path": "/data/downloads/", "download_path": "/data/downloads/incomplete"}])
    fake.on("GET", f"{QBT}/torrents/files", [{"index": 0, "name": "Film.2025/Film.2025.nfo", "size": 100},
                                             {"index": 1, "name": name, "size": size}])
    fake.on("GET", f"{QBT}/torrents/pieceStates", [2] * have_pieces + [0] * (total_pieces - have_pieces))
    fake.on("POST", f"{QBT}/torrents/toggleSequentialDownload", handler=lambda r: (calls.append("seq"), httpx.Response(200))[1])
    fake.on("POST", f"{QBT}/torrents/toggleFirstLastPiecePrio", handler=lambda r: (calls.append("fl"), httpx.Response(200))[1])
    return data


def _request(status="downloading"):
    from continuo.video import db
    req = db.create(kind="movie", tmdb_id=603, title="Film", year=2025, poster_path=None, user_id=1)
    db.update(req["id"], status=status, radarr_movie_id=7)
    return req["id"]


def test_starting_play_now_requests_the_film(vclient, fake):
    fake.on("GET", f"{TMDB}/movie/603", {"id": 603, "title": "The Matrix", "release_date": "1999-03-31"})
    fake.on("GET", f"{RADARR}/movie", [])
    fake.on("GET", f"{RADARR}/movie/lookup/tmdb", {"tmdbId": 603, "title": "The Matrix"})
    fake.on("POST", f"{RADARR}/movie", {"id": 7})
    make_user()
    s = login(vclient)
    r = vclient.post("/v1/video/play-now", json={"tmdbId": 603}, headers=bearer(s)).json()
    assert r["state"] == "searching" and r["requestId"] and r["created"] is True
    again = vclient.post("/v1/video/play-now", json={"tmdbId": 603}, headers=bearer(s)).json()
    assert again["requestId"] == r["requestId"] and again["created"] is False


def test_downloading_then_ready_with_a_signed_link(vclient, fake, monkeypatch):
    make_user()
    s = login(vclient)
    rid = _request()
    from continuo.video import playnow
    monkeypatch.setattr(playnow, "READY_MIN_BYTES", 2 * PIECE)
    toggles: list = []
    size = 100 * PIECE
    _torrent(fake, vclient, size, have_pieces=1, total_pieces=101, toggles=toggles)
    st = vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()
    assert st["state"] == "downloading" and st["contiguousBytes"] == PIECE - 100, "the film starts 100 bytes into the torrent"
    assert toggles == ["seq"], "sequential switched on; first/last was already on"
    _torrent(fake, vclient, size, have_pieces=5, total_pieces=101)
    st = vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()
    assert st["state"] == "ready" and st["container"] == "mkv" and st["mime"] == "video/x-matroska"
    assert st["streamUrl"].startswith(f"https://api.example/v1/video/play-now/{rid}/stream?u=1&exp=")


def test_the_stream_serves_only_what_has_arrived(vclient, fake, monkeypatch):
    make_user()
    s = login(vclient)
    rid = _request()
    from continuo.video import playnow
    monkeypatch.setattr(playnow, "READY_MIN_BYTES", 2 * PIECE)
    monkeypatch.setattr(playnow, "WAIT_SECONDS", 0)
    size = 100 * PIECE
    data = _torrent(fake, vclient, size, have_pieces=5, total_pieces=101)
    url = vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()["streamUrl"].replace("https://api.example", "")
    r = vclient.get(url, headers={"Range": "bytes=0-"})
    have = 5 * PIECE - 100
    assert r.status_code == 206 and r.headers["content-range"] == f"bytes 0-{have - 1}/{size}"
    assert r.content == data[:have] and r.headers["content-type"] == "video/x-matroska"
    r = vclient.get(url, headers={"Range": "bytes=10-19"})
    assert r.content == data[10:20]
    r = vclient.get(url, headers={"Range": f"bytes={60 * PIECE}-"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "still_downloading", "a jump past what's arrived waits, then says so"
    assert vclient.get(url.split("sig=")[0] + "sig=" + "0" * 64).status_code == 403
    assert vclient.get(url.replace("u=1", "u=2")).status_code == 403, "signed for one account"


def test_in_the_library_means_jellyfin(vclient, fake):
    from continuo.video import index
    fake.on("GET", "http://jellyfin.test/Items", {"Items": [{"Id": "jf-603", "Type": "Movie", "Name": "Film", "ProviderIds": {"Tmdb": "603"}}]})
    run(index.refresh())
    make_user()
    s = login(vclient)
    rid = _request()
    assert vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()["state"] == "available"
    assert vclient.post("/v1/video/play-now", json={"tmdbId": 603}, headers=bearer(s)).json() == \
        {"state": "available", "jellyfinId": "jf-603", "requestId": None}


def test_a_file_outside_the_downloads_folder_is_never_served(vclient, fake):
    from continuo.video import playnow
    t = {"save_path": "/data/downloads/../../etc", "download_path": None}
    assert playnow.local_path(t, "passwd") == ""
    assert playnow.local_path({"save_path": "/elsewhere"}, "x.mkv") == ""


def test_qbittorrent_signs_in_when_asked(vclient, fake):
    from continuo.video.upstream import qbittorrent
    state = {"in": False}

    def info(r):
        return httpx.Response(200, json=[]) if state["in"] else httpx.Response(403)

    def login_(r):
        state["in"] = True
        return httpx.Response(200, text="Ok.")
    fake.on("GET", f"{QBT}/torrents/info", handler=info)
    fake.on("POST", f"{QBT}/auth/login", handler=login_)
    assert run(qbittorrent.torrent(HASH)) is None
    sent = fake.called("POST", f"{QBT}/auth/login")[0].content.decode()
    assert state["in"] is True and "username=admin" in sent and "password=pw" in sent


SONARR = "http://sonarr.test/api/v3"


def test_which_episode_a_file_is():
    from continuo.video.playnow import episode_in_name
    assert episode_in_name("Show.S01E02.1080p.mkv") == (1, 2)
    assert episode_in_name("Show/Season 1/show.s1e10.mkv") == (1, 10)
    assert episode_in_name("Show - 2x07 - Title.mp4") == (2, 7)
    assert episode_in_name("Show.S01E02E03.mkv") == (1, 2)
    assert episode_in_name("Show.1080p.x264.mkv") is None
    assert episode_in_name("Film.2025.mkv") is None


def test_an_episode_in_a_season_pack(vclient):
    from continuo.video.playnow import pick_file
    t = {"piece_size": PIECE, "save_path": "/data/downloads"}
    files = [{"index": 0, "name": "Show.S01/Show.S01E01.mkv", "size": 5000}, {"index": 1, "name": "Show.S01/Show.S01E02.mkv", "size": 6000},
             {"index": 2, "name": "Show.S01/Show.S01E03.mkv", "size": 7000}]
    lay = pick_file(t, files, (1, 2))
    assert (lay.name, lay.offset, lay.size) == ("Show.S01/Show.S01E02.mkv", 5000, 6000)
    assert pick_file(t, files, (1, 9)) is None, "not in this pack"
    assert pick_file(t, [{"index": 0, "name": "show.ep.mkv", "size": 9000}, {"index": 1, "name": "sample.mkv", "size": 100}], (1, 4)).name == "show.ep.mkv", \
        "the only real video, whatever its name"


def test_play_now_for_an_episode(vclient, fake, monkeypatch):
    from continuo.video import db, playnow
    monkeypatch.setattr(playnow, "READY_MIN_BYTES", 2 * PIECE)
    config.reload(dict(os.environ, **{k: v for k, v in dict(CONTINUO_MODULES="music,video", JELLYFIN_URL="http://jellyfin.test",
        JELLYFIN_API_KEY="jf", SONARR_URL="http://sonarr.test", SONARR_API_KEY="so", QBITTORRENT_URL="http://qbt.test",
        VIDEO_DOWNLOADS_DIR=str(vclient.downloads), QBITTORRENT_DOWNLOADS_DIR="/data/downloads", PUBLIC_BASE_URL="https://api.example").items()}))
    make_user()
    s = login(vclient)
    req = db.create(kind="series", tmdb_id=1396, title="Show", year=2008, poster_path=None, user_id=1, seasons=[1])
    db.update(req["id"], status="downloading", sonarr_series_id=5)
    pack = vclient.downloads / "incomplete" / "Show.S01"
    pack.mkdir()
    e1, e2 = bytes(range(256)) * 12, bytes(reversed(range(256))) * 16          # 3072 and 4096 bytes
    (pack / "Show.S01E01.mkv").write_bytes(e1)
    (pack / "Show.S01E02.mkv").write_bytes(e2)
    fake.on("GET", f"{SONARR}/queue/details", [{"downloadId": HASH, "protocol": "torrent", "episode": {"seasonNumber": 1, "episodeNumber": 1}},
                                               {"downloadId": HASH, "protocol": "torrent", "episode": {"seasonNumber": 1, "episodeNumber": 2}}])
    fake.on("GET", f"{QBT}/torrents/info", [{"hash": HASH.lower(), "piece_size": PIECE, "seq_dl": True, "f_l_piece_prio": True,
                                             "save_path": "/data/downloads", "download_path": "/data/downloads/incomplete"}])
    fake.on("GET", f"{QBT}/torrents/files", [{"index": 0, "name": "Show.S01/Show.S01E01.mkv", "size": len(e1)},
                                             {"index": 1, "name": "Show.S01/Show.S01E02.mkv", "size": len(e2)}])
    fake.on("GET", f"{QBT}/torrents/pieceStates", [2] * 7)                    # all 7 KiB here
    st = vclient.get(f"/v1/video/play-now/{req['id']}", params={"season": 1, "episode": 2}, headers=bearer(s)).json()
    assert st["state"] == "ready" and "&s=1&e=2&" in st["streamUrl"]
    assert fake.called("GET", f"{SONARR}/queue/details")[0].url.params["includeEpisode"] == "true"
    url = st["streamUrl"].replace("https://api.example", "")
    assert vclient.get(url, headers={"Range": "bytes=0-"}).content == e2, "episode 2, not the pack's first file"
    assert vclient.get(url.replace("&e=2&", "&e=1&")).status_code == 403, "the link is for that episode only"
    assert vclient.post("/v1/video/play-now", json={"tmdbId": 1396, "kind": "series"}, headers=bearer(s)).status_code == 400


def test_play_now_for_another_season_widens_the_request(vclient, fake):
    from continuo.video import db, playnow_api
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", SONARR_URL="http://sonarr.test", SONARR_API_KEY="so", TMDB_API_KEY="tm"))
    req = db.create(kind="series", tmdb_id=1396, title="Show", year=2008, poster_path=None, user_id=None, seasons=[1])
    fake.on("GET", f"{TMDB}/tv/1396", {"id": 1396, "name": "Show", "external_ids": {"tvdb_id": 81189}})
    fake.on("GET", f"{SONARR}/series", [{"id": 5, "tvdbId": 81189, "seasons": [{"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": False}]}])
    fake.on("PUT", f"{SONARR}/series/5", handler=lambda r: fake.body(r))
    fake.on("POST", f"{SONARR}/command", {"id": 1})
    run(playnow_api._ensure_season(req["id"], 2))
    assert db.get(req["id"])["seasons"] == "[1, 2]"
    assert [fake.body(c)["seasonNumber"] for c in fake.called("POST", f"{SONARR}/command")] == [2]


def test_the_download_mode_is_flipped_once_not_back(vclient, fake):
    """Two checks at once mustn't each flip the toggle (it ended up off on the homelab)."""
    import asyncio

    from continuo.video.upstream import qbittorrent
    state = {"seq": False, "fl": False}
    flips: list = []
    fake.on("GET", f"{QBT}/torrents/info", handler=lambda r: httpx.Response(200, json=[{"hash": HASH.lower(), "seq_dl": state["seq"], "f_l_piece_prio": state["fl"]}]))

    def flip(name, key):
        def h(r):
            flips.append(name)
            state[key] = not state[key]
            return httpx.Response(200)
        return h
    fake.on("POST", f"{QBT}/torrents/toggleSequentialDownload", handler=flip("seq", "seq"))
    fake.on("POST", f"{QBT}/torrents/toggleFirstLastPiecePrio", handler=flip("fl", "fl"))
    stale = {"seq_dl": False, "f_l_piece_prio": False}

    async def both():
        await asyncio.gather(qbittorrent.stream_mode(HASH, stale), qbittorrent.stream_mode(HASH, stale), qbittorrent.stream_mode(HASH, stale))
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(qbittorrent, "SETTLE_CHECK_SECONDS", 0)
    try:
        run(both())
    finally:
        mp.undo()
    assert flips == ["seq", "fl"] and state == {"seq": True, "fl": True}


def test_the_stream_remembers_where_the_file_is(vclient, fake, monkeypatch):
    make_user()
    s = login(vclient)
    rid = _request()
    from continuo.video import playnow
    monkeypatch.setattr(playnow, "READY_MIN_BYTES", 2 * PIECE)
    _torrent(fake, vclient, 100 * PIECE, have_pieces=50, total_pieces=101)
    url = vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()["streamUrl"].replace("https://api.example", "")
    before = len(fake.called("GET", f"{RADARR}/queue/details"))
    for n in range(4):
        assert vclient.get(url, headers={"Range": f"bytes={n * PIECE}-{n * PIECE + 99}"}).status_code == 206
    assert len(fake.called("GET", f"{RADARR}/queue/details")) - before == 1, "the queue asked once, not for every chunk"
    assert len(fake.called("GET", f"{QBT}/torrents/pieceStates")) >= 4, "what has arrived is still checked every time"


def _rel(title, seeds, gb, quality="Bluray-1080p", resolution=1080, rejections=(), guid=None):
    return {"title": title, "seeders": seeds, "size": int(gb * 1e9), "protocol": "torrent", "guid": guid or title, "indexerId": 3,
            "quality": {"quality": {"name": quality, "resolution": resolution}}, "rejections": list(rejections)}


# Shaped like the homelab's real search for The Shawshank Redemption (1 Nov 2026).
SHAWSHANK = [
    _rel("The Shawshank Redemption 1994 REPACK UHD BluRay 1080p DDP 5 1 DoVi HDR10 x265-SM737", 2, 8.5),
    _rel("The Shawshank Redemption (1994) 1080p BrRip - 1.6GB -YIFY", 714, 1.7, rejections=["Quality for release in queue already meets cutoff: Bluray-1080p v2"]),
    _rel("The Shawshank Redemption 1994 REMASTERED 1080p BluRay H264 AAC LAMA[TGx]", 171, 2.9, rejections=["Quality for release in queue already meets cutoff: Bluray-1080p v2"]),
    _rel("The Shawshank Redemption 1994 1080p BluRay x265 AAC 5 1 88", 294, 2.4),
    _rel("The Shawshank Redemption 1994 bluray hdr 2160p av1 5 1 opus subs Dust", 152, 8.0, "Bluray-2160p", 2160),
    _rel("The.Shawshank.Redemption.1994.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC.DTS-HD.MA.5.1-F", 104, 58.3, "Remux-2160p", 2160),
    _rel("The Shawshank Redemption 1994 1080p WEB H264 blocked", 400, 3.0, rejections=["Release is blocklisted"]),
]


def test_play_now_picks_a_release_to_stream():
    from continuo.video.playnow import best_release, streamable
    assert streamable(SHAWSHANK[0], 142) is None, "2 sources, and Dolby Vision"
    assert streamable(SHAWSHANK[4], 142) is None and streamable(SHAWSHANK[5], 142) is None, "4K, a remux"
    assert streamable(SHAWSHANK[6], 142) is None, "blocklisted"
    assert streamable(SHAWSHANK[2], 142) is not None, "a release already queued doesn't rule another out"
    assert best_release(SHAWSHANK, 142)["title"].endswith("LAMA[TGx]"), "well seeded, H.264, a sensible bitrate"
    assert best_release(SHAWSHANK[:1], 142) is None


def test_a_new_play_now_grabs_its_own_release(vclient, fake):
    fake.on("GET", f"{TMDB}/movie/278", {"id": 278, "title": "The Shawshank Redemption", "release_date": "1994-09-23"})
    fake.on("GET", f"{RADARR}/movie", [])
    fake.on("GET", f"{RADARR}/movie/lookup/tmdb", {"tmdbId": 278, "title": "The Shawshank Redemption"})
    fake.on("POST", f"{RADARR}/movie", {"id": 38, "runtime": 142})
    fake.on("GET", f"{RADARR}/release", SHAWSHANK)
    fake.on("POST", f"{RADARR}/release", {"approved": True})
    make_user()
    s = login(vclient)
    vclient.post("/v1/video/play-now", json={"tmdbId": 278}, headers=bearer(s))
    for _ in range(50):
        if fake.called("POST", f"{RADARR}/release"):
            break
        import time
        time.sleep(0.05)
    added = fake.body(fake.called("POST", f"{RADARR}/movie")[0])
    assert added["addOptions"] == {"searchForMovie": False}, "Radarr doesn't pick: Play now does"
    assert fake.body(fake.called("POST", f"{RADARR}/release")[0])["guid"].endswith("LAMA[TGx]")


def test_a_slow_download_is_swapped_for_a_faster_one(vclient, fake):
    make_user()
    s = login(vclient)
    rid = _request()
    fake.on("GET", f"{RADARR}/movie/7", {"id": 7, "runtime": 142})
    fake.on("GET", f"{RADARR}/release", SHAWSHANK)
    fake.on("GET", f"{RADARR}/queue/details", [{"id": 91, "movieId": 7, "downloadId": HASH, "protocol": "torrent", "title": SHAWSHANK[0]["title"]}])
    fake.on("DELETE", f"{RADARR}/queue/91", {})
    fake.on("POST", f"{RADARR}/release", {"approved": True})
    r = vclient.post(f"/v1/video/play-now/{rid}/faster", headers=bearer(s))
    assert r.status_code == 200 and r.json()["title"].endswith("LAMA[TGx]")
    dropped = fake.called("DELETE", f"{RADARR}/queue/91")[0].url.params
    assert (dropped["removeFromClient"], dropped["blocklist"]) == ("true", "true")
    fake.on("GET", f"{RADARR}/release", SHAWSHANK[:1])
    assert vclient.post(f"/v1/video/play-now/{rid}/faster", headers=bearer(s)).status_code == 404


def test_too_slow_to_watch_says_so(vclient, fake, monkeypatch):
    make_user()
    s = login(vclient)
    rid = _request()
    _torrent(fake, vclient, 100 * PIECE, have_pieces=1, total_pieces=101)
    fake.on("GET", f"{QBT}/torrents/info", [{"hash": HASH.lower(), "piece_size": PIECE, "progress": 0.02, "dlspeed": 0, "time_active": 300,
                                             "seq_dl": True, "f_l_piece_prio": True, "save_path": "/data/downloads/",
                                             "download_path": "/data/downloads/incomplete"}])
    assert vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()["tooSlow"] is True


def test_a_thin_first_answer_is_searched_again(vclient, fake, monkeypatch):
    """Interstellar: the first answer (one indexer, seconds in) had only a 10-seed copy."""
    from continuo.video import playnow
    monkeypatch.setattr(playnow, "RESEARCH_SECONDS", 0)
    calls = {"n": 0}
    thin = [_rel("Interstellar 2014 1080p Bluray x264-SURGE", 10, 6.4)]
    full = thin + [_rel("Interstellar 2014 1080p ATVP WEB DL DD 5 1 H 264 PiRaTeS", 88, 8.9)]

    def releases(r):
        calls["n"] += 1
        return httpx.Response(200, json=thin if calls["n"] == 1 else full)
    fake.on("GET", f"{RADARR}/release", handler=releases)
    fake.on("POST", f"{RADARR}/release", {"approved": True})
    chosen = run(playnow.pick_and_grab({"id": 40, "runtime": 169}))
    assert calls["n"] == 2 and chosen["title"].endswith("PiRaTeS")
    calls["n"] = 0
    fake.on("GET", f"{RADARR}/release", thin)
    assert run(playnow.pick_and_grab({"id": 40, "runtime": 169}))["title"].endswith("SURGE"), "nothing better: settle for fewer"


def test_a_copy_with_few_real_sources_is_swapped_once(vclient, fake):
    make_user()
    s = login(vclient)
    rid = _request()
    _torrent(fake, vclient, 100 * PIECE, have_pieces=1, total_pieces=101)
    fake.on("GET", f"{QBT}/torrents/info", [{"hash": HASH.lower(), "piece_size": PIECE, "progress": 0.05, "dlspeed": 10**9, "time_active": 120,
                                             "num_seeds": 2, "seq_dl": True, "f_l_piece_prio": True, "save_path": "/data/downloads/",
                                             "download_path": "/data/downloads/incomplete"}])
    fake.on("GET", f"{RADARR}/queue/details", [{"id": 91, "movieId": 7, "downloadId": HASH, "protocol": "torrent", "title": "Thin.Copy.1080p.x264"}])
    fake.on("GET", f"{RADARR}/movie/7", {"id": 7, "runtime": 142})
    fake.on("GET", f"{RADARR}/release", [_rel("Thin.Copy.1080p.x264", 40, 3.0), *SHAWSHANK])
    fake.on("DELETE", f"{RADARR}/queue/91", {})
    fake.on("POST", f"{RADARR}/release", {"approved": True})
    st = vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s)).json()
    assert st["state"] == "searching" and "switched" in st["note"]
    assert fake.body(fake.called("POST", f"{RADARR}/release")[0])["guid"].endswith("LAMA[TGx]"), "not the thin one again"
    vclient.get(f"/v1/video/play-now/{rid}", headers=bearer(s))
    assert len(fake.called("POST", f"{RADARR}/release")) == 1, "swapped once, not every look"
