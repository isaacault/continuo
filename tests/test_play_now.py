"""Play now (#12): the fastest copy, played while it downloads, then cached; Keep files it."""
import asyncio
import dataclasses
import os

import pytest
from conftest import bearer, login, make_user

from continuo import config
from continuo.db import cache as cache_db
from continuo.db import requests as req_db
from continuo.db import streams as streams_db
from continuo.fetch import soulseek
from continuo.library import tags
from continuo.requests import playnow, tracks
from continuo.upstream import musicbrainz

REC = "11111111-0000-4000-8000-000000000001"
RG = "22222222-0000-4000-8000-000000000002"
REMOTE = "@@music\\Peach Pit\\Being So Normal\\02 Drop the Guillotine.flac"
AUDIO = b"fLaC" + bytes(range(256)) * 2048          # 512 KiB


def _hit(user, free=True, speed=5_000_000):
    return {"username": user, "hasFreeUploadSlot": free, "queueLength": 0, "uploadSpeed": speed,
            "files": [{"filename": REMOTE, "size": len(AUDIO), "extension": "flac", "length": 231}]}


ORIGINAL_SEARCH = soulseek.search


@pytest.fixture
def world(tmp_path, monkeypatch, fake):
    media = tmp_path / "media"
    store, dl, inc = media / "music", media / "downloads" / "soulseek", media / "downloads" / "soulseek-incomplete"
    for d in (store, dl, inc):
        d.mkdir(parents=True)
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, music_dir=str(store), slskd_downloads_dir=str(dl), slskd_incomplete_dir=str(inc),
        slskd_url="http://slskd.test", slskd_api_key="k"))
    monkeypatch.setattr(playnow, "POLL_S", 0.0)
    state = {"transfer": {}, "enqueued": [], "responses": [_hit("alice")]}

    async def search(text, quiet_s=15.0, max_s=None):
        return state["responses"]

    async def enqueue(c):
        state["enqueued"].append(c.username)

    async def transfer(c):
        step = state["transfer"].get(c.username)
        return step(c) if callable(step) else step
    monkeypatch.setattr(soulseek, "search", search)
    monkeypatch.setattr(soulseek, "enqueue", enqueue)
    monkeypatch.setattr(soulseek, "transfer", transfer)
    fake.on("GET", "http://navidrome.test/rest/startScan", {"subsonic-response": {"status": "ok", "scanStatus": {}}})
    return {"dl": dl, "inc": inc, "store": store, "state": state}


def _downloading(world, user="alice", have=len(AUDIO)):
    """slskd writing the copy into its incomplete folder, [have] bytes so far."""
    d = world["inc"] / user / "music" / "Peach Pit" / "Being So Normal"
    d.mkdir(parents=True, exist_ok=True)
    (d / "02 Drop the Guillotine.flac").write_bytes(AUDIO[:have])
    return d / "02 Drop the Guillotine.flac"


def _finished(world, partial):
    """slskd moving the finished copy to its downloads folder (under the remote parent folder)."""
    d = world["dl"] / "Being So Normal"
    d.mkdir(parents=True, exist_ok=True)
    os.replace(partial, d / "02 Drop the Guillotine.flac")


def _run(user_id):
    async def go():
        row = playnow.start(user_id, REC, RG, "Drop the Guillotine", "Peach Pit", 230000)
        await asyncio.gather(*playnow._tasks)
        return streams_db.get(row["id"])
    return asyncio.run(go())


def test_the_fastest_copy_streams_while_it_downloads_then_is_cached(world):
    isaac = make_user()
    seen = {}

    def progress(c):
        sid = _ids()[0]
        if "partial" not in seen:
            seen["partial"] = _downloading(world, have=300 * 1024)     # past START_BYTES
            return {"state": "InProgress", "id": "t1", "percentComplete": 58}
        seen["while_downloading"] = playnow.view(streams_db.get(sid))
        seen["partial"].write_bytes(AUDIO)
        _finished(world, seen["partial"])
        return {"state": "Completed, Succeeded", "id": "t1"}
    world["state"]["transfer"]["alice"] = progress
    row = _run(isaac["id"])
    early = seen["while_downloading"]
    assert early["status"] == "streaming" and early["playable"] and early["bytes"] == 300 * 1024, \
        "playable once the first bytes are on disk"
    assert row["status"] == "ready" and world["state"]["enqueued"] == ["alice"]
    item = cache_db.get(row["cache_item_id"])
    assert item["origin"] == "stream" and item["bytes"] == len(AUDIO)
    assert not (world["dl"] / "Being So Normal").exists(), "slskd's folder is left clean"
    again = asyncio.run(_start_again(isaac["id"]))
    assert again["id"] == row["id"], "a song still cached plays again at once, nothing fetched"


def _ids():
    from continuo.db.conn import connect
    with connect() as c:
        return [r[0] for r in c.execute("SELECT id FROM streams ORDER BY created_at")]


async def _start_again(user_id):
    return playnow.start(user_id, REC, RG, "Drop the Guillotine", "Peach Pit", 230000)


def test_a_rejected_copy_gives_way_to_the_next(world):
    isaac = make_user()
    world["state"]["responses"] = [_hit("stavriani"), _hit("jagodka", free=False, speed=100)]
    world["state"]["transfer"]["stavriani"] = {"state": "Completed, Rejected", "id": "t1"}

    def ok(c):
        _finished(world, _downloading(world, "jagodka"))
        return {"state": "Completed, Succeeded", "id": "t2"}
    world["state"]["transfer"]["jagodka"] = ok
    row = _run(isaac["id"])
    assert world["state"]["enqueued"] == ["stavriani", "jagodka"] and row["status"] == "ready"


def test_nobody_has_it(world):
    isaac = make_user()
    world["state"]["responses"] = []
    row = _run(isaac["id"])
    assert row["status"] == "failed" and "Couldn't find" in row["error"]


def test_nobody_could_send_it(world):
    isaac = make_user()
    world["state"]["responses"] = [_hit("stavriani")]
    world["state"]["transfer"]["stavriani"] = {"state": "Completed, Rejected", "id": "t1"}
    row = _run(isaac["id"])
    assert row["status"] == "failed" and "Nobody on Soulseek could send" in row["error"]


def test_the_audio_is_served_with_ranges(client, world):
    isaac = make_user()

    def done(c):
        _finished(world, _downloading(world))
        return {"state": "Completed, Succeeded", "id": "t1"}
    world["state"]["transfer"]["alice"] = done
    row = _run(isaac["id"])
    s = login(client)
    full = client.get(f"/v1/play-now/{row['id']}/audio", headers=bearer(s))
    assert full.status_code == 200 and full.content == AUDIO and full.headers["content-type"] == "audio/flac"
    part = client.get(f"/v1/play-now/{row['id']}/audio", headers={**bearer(s), "Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == AUDIO[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(AUDIO)}"
    status = client.get(f"/v1/play-now/{row['id']}", headers=bearer(s)).json()
    assert status["playable"] and status["percent"] == 100.0


def test_someone_else_s_stream_is_not_yours(client, world):
    make_user("guest", role="user")
    owner = make_user("owner", role="user")
    row = streams_db.create("sid-x", owner["id"], REC, RG, "T", "A", None)
    assert client.get(f"/v1/play-now/{row['id']}", headers=bearer(login(client, "owner"))).status_code == 200
    assert client.get(f"/v1/play-now/{row['id']}", headers=bearer(login(client, "guest"))).status_code == 404


def test_keep_files_the_cached_copy_as_a_single(client, world, monkeypatch):
    isaac = make_user()

    def done(c):
        _finished(world, _downloading(world))
        return {"state": "Completed, Succeeded", "id": "t1"}
    world["state"]["transfer"]["alice"] = done
    row = _run(isaac["id"])

    async def recording(mbid):
        return {"title": "Drop the Guillotine", "artist": "Peach Pit", "artistId": "pp", "durationMs": 230000}

    async def album_info(mbid):
        return {"title": "Being So Normal", "artist": "Peach Pit", "artistId": "pp", "date": "2017", "year": 2017,
                "releaseId": "rel", "imageUrl": None,
                "tracks": [{"position": 2, "title": "Drop the Guillotine", "discNumber": 1, "musicBrainzRecordingId": REC}]}
    monkeypatch.setattr(musicbrainz, "recording", recording)
    monkeypatch.setattr(musicbrainz, "album_info", album_info)
    monkeypatch.setattr(tags, "write_track_tags", lambda p, f: True)
    searched = []

    async def no_search(*a, **k):
        searched.append(1)
        return []
    monkeypatch.setattr(soulseek, "search", no_search)
    s = login(client)
    out = client.post(f"/v1/play-now/{row['id']}/keep", headers=bearer(s))
    assert out.status_code == 200, out.text
    asyncio.run(tracks.tick())          # MusicBrainz details
    asyncio.run(tracks.tick())          # filed from the cache
    req = req_db.get(out.json()["id"])
    assert req["status"] == "scanning" and req["file_path"].startswith("_singles/Peach Pit/Being So Normal/")
    assert (world["store"] / req["file_path"]).read_bytes() == AUDIO
    assert searched == [], "nothing is downloaded again"
    assert playnow.audio_path(streams_db.get(row["id"])), "the cache keeps its copy until evicted"


def test_keeping_a_copy_below_the_library_s_formats_fetches_a_better_one(client, world, monkeypatch):
    isaac = make_user()

    def done(c):
        _finished(world, _downloading(world))
        return {"state": "Completed, Succeeded", "id": "t1"}
    world["state"]["transfer"]["alice"] = done
    row = _run(isaac["id"])
    # Play now took a 192 kbps MP3: 230 s at 192 kbit/s.
    streams_db.update(row["id"], extension="mp3", size=230 * 192 * 1000 // 8, duration_ms=230_000)
    s = login(client)
    out = client.post(f"/v1/play-now/{row['id']}/keep", headers=bearer(s))
    assert out.status_code == 200, out.text
    req = req_db.get(out.json()["id"])
    assert not (req.get("fetch") or {}).get("local"), "searched for like any song request, in the library's formats"
    assert playnow.library_quality({"extension": "flac", "size": 1, "duration_ms": 1})
    assert playnow.library_quality({"extension": "mp3", "size": 230 * 320 * 1000 // 8, "duration_ms": 230_000})


def test_soulseek_offline_says_so_rather_than_nobody_has_it(client, world, fake, monkeypatch):
    isaac = make_user()
    monkeypatch.setattr(soulseek, "search", ORIGINAL_SEARCH)
    fake.on("POST", "http://slskd.test/api/v0/searches", status=409,
            json_body="The server connection must be connected and logged in to perform a search (currently: Disconnecting)")
    row = _run(isaac["id"])
    assert row["status"] == "failed" and "offline" in row["error"]


def test_a_song_request_waits_out_soulseek_being_offline(monkeypatch):
    async def offline(texts, **_kw):
        raise soulseek.Offline("currently: Disconnecting")
    monkeypatch.setattr(soulseek, "search_any", offline)
    seen = {}

    def status(row, s, **fields):
        seen.update(fields, status=s)
        return row
    monkeypatch.setattr(tracks, "_status", status)
    asyncio.run(tracks._search({"id": 1, "track": {"title": "Loaded", "artist": "Primal Scream"}, "search_cycles": 4}))
    assert seen["status"] == "searching" and "search_cycles" not in seen, "an outage isn't a search that found nothing"


def test_audio_waits_for_bytes_still_downloading(client, world):
    """The player reads ahead of the download: the response waits for slskd's next bytes."""
    import threading
    import time
    isaac = make_user()
    partial = _downloading(world, have=len(AUDIO) // 2)
    row = streams_db.create("sid-grow", isaac["id"], REC, RG, "Drop the Guillotine", "Peach Pit", None)
    streams_db.update(row["id"], status="streaming", username="alice", filename=REMOTE, size=len(AUDIO), extension="flac")

    def finish_download():
        time.sleep(0.6)
        with open(partial, "ab") as f:
            f.write(AUDIO[len(AUDIO) // 2:])
    threading.Thread(target=finish_download, daemon=True).start()
    out = client.get(f"/v1/play-now/{row['id']}/audio", headers=bearer(login(client)))
    assert out.status_code == 200 and out.headers["content-length"] == str(len(AUDIO))
    assert out.content == AUDIO, "the second half arrived while the request was waiting"


def _queue_three(user_id, order=None):
    """Three songs queued (polyphony#164); returns their statuses before any fetch has run."""
    async def go():
        ids = [playnow.start(user_id, f"{REC[:-1]}{i}", RG, f"Song {i}", "Peach Pit", None, queued=True)["id"]
               for i in range(3)]
        first = {sid: streams_db.get(sid)["status"] for sid in ids}
        for t in list(playnow._tasks):
            t.cancel()
        return ids, first
    return asyncio.run(go())


def test_queued_songs_are_fetched_two_at_a_time_soonest_first(world):
    isaac = make_user()
    ids, status = _queue_three(isaac["id"])
    assert [status[i] for i in ids] == ["searching", "searching", "queued"]


def test_the_app_s_queue_order_decides_what_s_fetched_next(world):
    isaac = make_user()
    ids = [streams_db.create(f"s{i}", isaac["id"], f"rec-{i}", RG, f"Song {i}", "A", None, status="queued")["id"]
           for i in range(3)]

    async def go():
        playnow.prioritise(isaac["id"], ["s2", "s0"])          # s2 plays first now
        out = {sid: streams_db.get(sid)["status"] for sid in ids}
        for t in list(playnow._tasks):
            t.cancel()
        return out
    out = asyncio.run(go())
    assert out == {"s2": "searching", "s0": "searching", "s1": "queued"}


def test_a_song_played_now_starts_whatever_is_queued(world):
    isaac = make_user()

    async def go():
        for i in range(3):
            playnow.start(isaac["id"], f"rec-q{i}", RG, f"Q{i}", "A", None, queued=True)
        now = playnow.start(isaac["id"], "rec-now", RG, "Now", "A", None)
        status = streams_db.get(now["id"])["status"]
        for t in list(playnow._tasks):
            t.cancel()
        return status
    assert asyncio.run(go()) == "searching"


def test_a_restart_puts_cut_off_fetches_back_in_the_queue(world):
    isaac = make_user()
    streams_db.create("cut", isaac["id"], "rec-cut", RG, "Cut off", "A", None, status="downloading")

    async def go():
        n = playnow.requeue_interrupted()
        status = streams_db.get("cut")["status"]
        for t in list(playnow._tasks):
            t.cancel()
        return n, status
    n, status = asyncio.run(go())
    assert n == 1 and status == "searching", "requeued, then started again"


def test_the_priority_route(client, world):
    isaac = make_user()
    streams_db.create("p0", isaac["id"], "rec-p0", RG, "P0", "A", None, status="queued")
    out = client.post("/v1/play-now/priority", headers=bearer(login(client)), json={"ids": ["p0"]})
    assert out.status_code == 200 and out.json()["updated"] == 1
