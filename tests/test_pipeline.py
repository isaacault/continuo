import time

from conftest import LIDARR, run

from continuo import config
from continuo.db import requests as req_db
from continuo.requests import pipeline
from continuo.workers import status


def _row(**kw):
    row = req_db.create(mb_release_group_id="rg-p", title="Geogaddi", artist="Boards of Canada",
                        year=2002, artwork_url=None, user_id=None)
    return req_db.update(row["id"], **{"lidarr_album_id": 412, "lidarr_artist_id": 9,
                                       "status": "searching", **kw})


def _album(fake, files=0, total=23, monitored=True):
    fake.on("GET", f"{LIDARR}/album/412", {
        "id": 412, "monitored": monitored, "releases": [{"foreignReleaseId": "r", "monitored": True}],
        "statistics": {"trackFileCount": files, "trackCount": total,
                       "percentOfTracks": 100 * files / total if total else 0}})


def test_stage_shape(fake):
    p = pipeline.build("searching", total=10, detail="x")
    assert p["stages"] == ["requested", "searching", "downloading", "importing", "scanning",
                           "available"]
    assert p["stageIndex"] == 1 and p["trackCount"] == 10


def test_searching_then_still_looking_detail(fake):
    _album(fake)
    row = _row()
    p = run(pipeline.compute(row, {}))
    assert p["stage"] == "searching" and "Searching" in p["detail"]
    row = req_db.update(row["id"], still_looking=True, search_cycles=14)
    p = run(pipeline.compute(row, {}))
    assert p["stage"] == "searching"
    assert p["detail"].startswith("Still looking — 14 search cycles")
    assert "continues automatically" in p["detail"]


def test_torrent_downloading_with_progress(fake):
    _album(fake)
    q = {412: {"id": 5, "albumId": 412, "protocol": "torrent", "downloadClient": "qBittorrent",
               "trackedDownloadState": "downloading", "trackedDownloadStatus": "ok",
               "size": 1000, "sizeLeft": 250, "timeLeft": "00:12:30", "messages": []}}
    p = run(pipeline.compute(_row(), q))
    assert p["stage"] == "downloading" and p["source"] == "torrent"
    assert p["percent"] == 75.0 and p["download"]["queueId"] == 5
    assert p["download"]["stalled"] is False and "12:30" in p["detail"]


def test_stalled_torrent(fake, monkeypatch):
    _album(fake)
    monkeypatch.setattr(config, "settings", config.settings.__class__(
        **{**config.settings.__dict__, "stalled_download_hours": 1}))
    q = {412: {"id": 6, "albumId": 412, "protocol": "torrent", "downloadClient": "qB",
               "trackedDownloadState": "downloading", "size": 1000, "sizeLeft": 900,
               "messages": []}}
    row = _row()
    assert run(pipeline.compute(row, q))["download"]["stalled"] is False
    pipeline._progress[6] = (900, time.monotonic() - 2 * 3600)
    p = run(pipeline.compute(row, q))
    assert p["download"]["stalled"] is True and "stalled" in p["detail"]


def test_import_blocked_shows_reason(fake):
    _album(fake)
    q = {412: {"id": 7, "albumId": 412, "protocol": "torrent", "trackedDownloadState":
               "importBlocked", "trackedDownloadStatus": "warning",
               "messages": ["Album match is not close enough"]}}
    p = run(pipeline.compute(_row(), q))
    assert p["stage"] == "importing" and "not close enough" in p["detail"]


def test_partial_files_importing_and_complete_scanning(fake):
    _album(fake, files=9)
    assert run(pipeline.compute(_row(), {}))["stage"] == "importing"
    _album(fake, files=23)
    from continuo.upstream import lidarr
    lidarr._album_cache.clear()
    p = run(pipeline.compute(req_db.get(1), {}))
    assert p["stage"] == "scanning" and p["percent"] == 100


def test_soulseek_progress_matches_album_not_just_artist(fake, monkeypatch):
    env = dict(config.settings.__dict__, slskd_api_key="k")
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    from continuo.upstream import http
    http.reset()
    _album(fake)
    fake.on("GET", "http://slskd.test/api/v0/transfers/downloads", [{
        "username": "peer", "directories": [{"files": [
            {"filename": "Boards of Canada/Geogaddi/01.flac", "size": 100,
             "bytesTransferred": 50, "state": "InProgress"},
            {"filename": "Boards of Canada/Tomorrow's Harvest/01.flac", "size": 100,
             "bytesTransferred": 100, "state": "Completed, Succeeded"},
        ]}]}])
    p = run(pipeline.compute(_row(), {}))
    assert p["stage"] == "downloading" and p["source"] == "soulseek"
    assert p["percent"] == 50.0 and "0/1 files" in p["detail"]


def test_status_loop_never_gives_up(fake, monkeypatch):
    env = dict(config.settings.__dict__, search_cycle_seconds=0, still_looking_after_cycles=6)
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    _album(fake)
    row = _row()
    for _ in range(50):
        run(status.update_one(req_db.get(row["id"]), {}))
    got = req_db.get(row["id"])
    assert got["status"] == "searching"          # never 'failed'
    assert got["search_cycles"] == 50 and got["still_looking"] is True
    assert [e["type"] for e in req_db.events(row["id"])].count("still_looking") == 1


def test_progress_resets_cycles(fake, monkeypatch):
    env = dict(config.settings.__dict__, search_cycle_seconds=0)
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    _album(fake)
    row = _row(search_cycles=9, still_looking=True)
    q = {412: {"id": 8, "albumId": 412, "protocol": "torrent", "trackedDownloadState":
               "downloading", "size": 10, "sizeLeft": 5, "messages": []}}
    run(status.update_one(row, q))
    got = req_db.get(row["id"])
    assert got["status"] == "downloading" and got["search_cycles"] == 0
    assert got["still_looking"] is False and got["last_source"] == "torrent"


def test_status_loop_hands_complete_album_to_scanning(fake):
    _album(fake, files=23)
    row = _row()
    run(status.update_one(row, {}))
    got = req_db.get(row["id"])
    assert got["status"] == "scanning" and got["scan_started_at"]


def test_latency_milestones_soulseek(fake, monkeypatch):
    """First transfer → download_started_at; first finished Soulseek file → first_file_at."""
    _album(fake)
    row = _row()
    downloading = pipeline.build("downloading", source="soulseek", download={"filesDone": 0}, row=row)
    first = status.latency_milestones(row, downloading)
    assert set(first) == {"download_started_at", "first_source"} and first["first_source"] == "soulseek"
    row = req_db.update(row["id"], **first)
    assert status.latency_milestones(row, downloading) == {}          # set once
    one_done = pipeline.build("downloading", source="soulseek", download={"filesDone": 1}, row=row)
    second = status.latency_milestones(row, one_done)
    assert set(second) == {"first_file_at"}
    row = req_db.update(row["id"], **second)
    assert status.latency_milestones(row, one_done) == {}


def test_latency_first_file_implies_download_started(fake):
    _album(fake)
    row = _row()
    imported = pipeline.build("importing", files=3, total=12, source="torrent", row=row)
    got = status.latency_milestones(row, imported)
    assert got["download_started_at"] == got["first_file_at"] and got["first_source"] == "torrent"


def test_latency_report_medians():
    from continuo.api.admin import latency_report
    rows = [
        {"first_source": "soulseek", "created_at": "2026-10-20T17:37:27+00:00",
         "download_started_at": "2026-10-20T17:43:36+00:00", "first_file_at": "2026-10-20T17:45:19+00:00",
         "available_at": "2026-10-20T18:02:51+00:00"},
        {"first_source": "soulseek", "created_at": "2026-10-21T10:00:00+00:00",
         "download_started_at": "2026-10-21T10:01:00+00:00", "first_file_at": None, "available_at": None},
        {"first_source": "torrent", "created_at": "2026-10-21T10:00:00+00:00",
         "download_started_at": "2026-10-21T10:00:30+00:00", "first_file_at": "2026-10-21T10:20:30+00:00",
         "available_at": "2026-10-21T10:22:00+00:00"},
    ]
    rep = latency_report(rows)
    s = rep["soulseek"]
    assert s["requests"] == 2
    assert s["toDownloadStart"] == {"n": 2, "median": 214.5, "p90": 338.1}   # 60 s and 369 s
    assert s["toFirstFile"] == {"n": 1, "median": 472.0, "p90": 472.0}
    assert rep["torrent"]["downloadToFirstFile"]["median"] == 1200.0
    assert rep["all"]["requests"] == 3
