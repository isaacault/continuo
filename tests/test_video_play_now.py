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
def vclient(fake, tmp_path):
    from fastapi.testclient import TestClient

    from continuo.main import create_app
    downloads = tmp_path / "downloads"
    (downloads / "incomplete" / "Film.2025").mkdir(parents=True)
    config.reload(dict(os.environ, CONTINUO_MODULES="music,video", JELLYFIN_URL="http://jellyfin.test", JELLYFIN_API_KEY="jf",
                       RADARR_URL="http://radarr.test", RADARR_API_KEY="rd", QBITTORRENT_URL="http://qbt.test",
                       QBITTORRENT_USERNAME="admin", QBITTORRENT_PASSWORD="pw", VIDEO_DOWNLOADS_DIR=str(downloads),
                       QBITTORRENT_DOWNLOADS_DIR="/data/downloads", PUBLIC_BASE_URL="https://api.example"))
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
