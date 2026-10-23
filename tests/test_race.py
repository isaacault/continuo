"""Racing slow downloads (#31): Soulseek starts when the torrent is slow; the first import wins."""
import asyncio

import pytest
from conftest import LIDARR, make_user

from continuo import config
from continuo.db import races as races_db
from continuo.db import requests as req_db
from continuo.fetch import soulseek
from continuo.requests import race
from continuo.upstream import slskd

SLSKD = "http://slskd.test/api/v0"
TITLES = ["Ernie", "Roady", "Hope", "Ray Ray", "Flashback", "Wandering Eye", "Del Fuego", "Dark Days", "This Room"]


def _resp(user, folder, names, free=True, queue=0, speed=2_000_000, ext="flac"):
    return {"username": user, "hasFreeUploadSlot": free, "queueLength": queue, "uploadSpeed": speed,
            "files": [{"filename": f"{folder}\\{i:02d} {n}.{ext}", "size": 30_000_000, "extension": ext,
                       "bitRate": 320 if ext == "mp3" else None} for i, n in enumerate(names, 1)]
            + [{"filename": f"{folder}\\cover.jpg", "size": 90_000, "extension": "jpg"}]}


RESPONSES = [
    _resp("half", r"@@a\Fat Freddy's Drop\Based on a True Story", TITLES[:4]),
    _resp("slowq", r"@@b\Music\Fat Freddy's Drop - Based on a True Story (2005)", TITLES, free=False, queue=30),
    _resp("good", r"@@c\FFD\Based On A True Story [FLAC]", TITLES),
    _resp("mp3", r"@@d\Fat Freddys Drop\Based on a True Story", TITLES, ext="mp3", speed=9_000_000),
    _resp("other", r"@@e\Fat Freddy's Drop\Dr Boondigga", TITLES),
]


def test_slow_means_the_first_minutes_say_it_won_t_finish_soon():
    m = 60
    assert not race.is_slow(0, 100, 0, 100, 5 * m), "inside the grace period"
    assert race.is_slow(0, 100, 0, 100, 11 * m), "nothing arrived for 10 minutes"
    assert race.is_slow(0, 1000, 10 * m, 990, 11 * m), "1% in 11 minutes: hours to go"
    assert not race.is_slow(0, 1000, 10 * m, 200, 11 * m), "80% in 11 minutes: minutes to go"


def test_the_best_whole_album_folder_wins():
    best = soulseek.best_album(RESPONSES, "Based on a True Story", TITLES, [("flac", 0), ("mp3", 320)])
    assert best.username == "good", "complete, FLAC, free slot; not half an album, not queued, not MP3"
    assert best.local_folder == "Based On A True Story [FLAC]"
    assert sum(1 for f in best.files if f.extension == "flac") == 9 and any(f.extension == "jpg" for f in best.files)


@pytest.fixture
def req(fake, monkeypatch):
    make_user()
    row = req_db.create(mb_release_group_id="rg-ffd", title="Based on a True Story", artist="Fat Freddy's Drop",
                        year=2005, artwork_url=None, user_id=1)
    row = req_db.update(row["id"], lidarr_album_id=619, status="downloading")
    monkeypatch.setattr(slskd, "enabled", lambda: True)
    fake.on("GET", f"{LIDARR}/track", [{"title": t} for t in TITLES])

    async def search(text, timeout_s=20.0):
        return RESPONSES
    monkeypatch.setattr(soulseek, "search", search)
    fake.on("POST", f"{SLSKD}/transfers/downloads/good", {}, status=201)
    return row


def test_a_slow_torrent_gets_soulseek_racing_it(req, fake, monkeypatch):
    torrent = {619: {"id": 77, "albumId": 619, "protocol": "torrent", "size": 427_000_000, "sizeLeft": 426_000_000}}
    clock = [1000.0]
    monkeypatch.setattr(race.time, "monotonic", lambda: clock[0])
    asyncio.run(race.tick([req], torrent))
    assert races_db.for_request(req["id"]) == [], "first sight: nothing to judge yet"
    clock[0] += 11 * 60
    torrent[619] = {**torrent[619], "sizeLeft": 425_900_000}
    asyncio.run(race.tick([req], torrent))
    (r,) = races_db.for_request(req["id"])
    assert r["state"] == "downloading" and r["username"] == "good"
    sent = fake.body(fake.called("POST", f"{SLSKD}/transfers/downloads/good")[0])
    assert len(sent) == 10
    clock[0] += 60
    asyncio.run(race.tick([req], torrent))
    assert len(races_db.for_request(req["id"])) == 1, "one race at a time"


def test_a_finished_racer_is_handed_to_lidarr(req, fake):
    r = race.races_db.add(req["id"], "soulseek", username="good", local_folder="Based On A True Story [FLAC]",
                          files=[{"filename": "a.flac", "size": 1}, {"filename": "b.flac", "size": 1}])
    fake.on("GET", f"{SLSKD}/transfers/downloads/good", {"directories": [{"files": [
        {"id": "t1", "filename": "a.flac", "state": "Completed, Succeeded"},
        {"id": "t2", "filename": "b.flac", "state": "Completed, Succeeded"}]}]})
    fake.on("POST", f"{LIDARR}/command", {"id": 5})
    asyncio.run(race.tick([], {}))
    cmd = fake.body(fake.called("POST", f"{LIDARR}/command")[0])
    assert cmd == {"name": "DownloadedAlbumsScan", "path": "/data/downloads/soulseek/Based On A True Story [FLAC]"}
    assert races_db.for_request(req["id"])[0]["state"] == "importing"
    assert r["id"]


def test_soulseek_winning_cancels_the_torrent_without_blocklisting(req, fake):
    races_db.add(req["id"], "soulseek", username="good", local_folder="x", state="importing")
    fake.on("GET", f"{LIDARR}/queue", {"records": [{"id": 77, "albumId": 619, "protocol": "torrent"}], "totalRecords": 1})
    fake.on("DELETE", f"{LIDARR}/queue/77", {})
    asyncio.run(race.on_imported([req_db.get(req["id"])], {"downloadclient": "", "eventtype": "Download"}))
    (d,) = fake.called("DELETE", f"{LIDARR}/queue/77")
    assert d.url.params["blocklist"] == "false" and d.url.params["removeFromClient"] == "true"
    assert races_db.for_request(req["id"])[0]["state"] == "won"


def test_the_torrent_winning_cancels_soulseek_and_clears_its_files(req, fake, tmp_path, monkeypatch):
    import dataclasses
    folder = tmp_path / "Based On A True Story [FLAC]"
    folder.mkdir()
    (folder / "01 Ernie.flac").write_bytes(b"x")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, slskd_downloads_dir=str(tmp_path)))
    races_db.add(req["id"], "soulseek", username="good", local_folder="Based On A True Story [FLAC]",
                 files=[{"filename": "a.flac", "size": 1}])
    fake.on("GET", f"{SLSKD}/transfers/downloads/good", {"directories": [{"files": [
        {"id": "t1", "filename": "a.flac", "state": "InProgress"}]}]})
    fake.on("DELETE", f"{SLSKD}/transfers/downloads/good/t1", {})
    asyncio.run(race.on_imported([req_db.get(req["id"])], {"downloadclient": "qBittorrent"}))
    assert fake.called("DELETE", f"{SLSKD}/transfers/downloads/good/t1")
    assert not folder.exists()
    assert races_db.for_request(req["id"])[0]["state"] == "lost"
