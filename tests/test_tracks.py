"""Single-song requests (#24): Soulseek straight into the song's album folder."""
import asyncio
import dataclasses

import pytest
from conftest import bearer, login, make_user

from continuo import config
from continuo.db import library as lib_db
from continuo.db import requests as req_db
from continuo.fetch import soulseek
from continuo.library import tags
from continuo.requests import tracks
from continuo.upstream import musicbrainz, navidrome

REC = "11111111-0000-4000-8000-000000000001"
RG = "22222222-0000-4000-8000-000000000002"


@pytest.fixture
def setup(tmp_path, monkeypatch, fake):
    store, dl = tmp_path / "media" / "music", tmp_path / "media" / "downloads" / "soulseek"
    store.mkdir(parents=True)
    dl.mkdir(parents=True)
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, music_dir=str(store), slskd_downloads_dir=str(dl), slskd_url="http://slskd.test",
        slskd_api_key="k"))

    async def recording(mbid):
        return {"title": "Drop the Guillotine", "artist": "Peach Pit", "artistId": "pp", "durationMs": 230000}

    async def album_info(mbid):
        return {"title": "Being So Normal", "artist": "Peach Pit", "artistId": "pp", "date": "2017-09-22",
                "year": 2017, "releaseId": "rel-1", "imageUrl": None,
                "tracks": [{"position": 1, "title": "Alrighty Aphrodite", "discNumber": 1, "musicBrainzRecordingId": "x"},
                           {"position": 2, "title": "Drop the Guillotine", "discNumber": 1, "musicBrainzRecordingId": REC}]}
    monkeypatch.setattr(musicbrainz, "recording", recording)
    monkeypatch.setattr(musicbrainz, "album_info", album_info)
    tagged = []
    monkeypatch.setattr(tags, "write_track_tags", lambda path, fields: tagged.append(fields) or True)
    fake.on("GET", "http://navidrome.test/rest/startScan", {"subsonic-response": {"status": "ok", "scanStatus": {}}})
    return store, dl, tagged


def _hit(user="alice", name="02 Drop the Guillotine.flac", folder="Peach Pit - Being So Normal (2017)"):
    return {"username": user, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1_000_000,
            "files": [{"filename": f"@@music\\{folder}\\{name}", "size": 30_000_000, "extension": "flac",
                       "length": 231}]}


def _request(client, user="isaac"):
    s = login(client, user)
    r = client.post("/v1/requests", headers=bearer(s), json={
        "kind": "track", "musicBrainzRecordingId": REC, "musicBrainzReleaseGroupId": RG})
    assert r.status_code == 201, r.text
    return r.json()


def test_a_song_request_is_its_own_request_and_never_reaches_lidarr(client, setup):
    make_user()
    out = _request(client)
    assert out["kind"] == "track" and out["title"] == "Drop the Guillotine"
    assert out["pipeline"]["stage"] == "requested"
    row = req_db.get(out["id"])
    assert row["album_release_group_id"] == RG and row["track"]["position"] == 2
    assert req_db.active_by_release_group() == {}, "album lookups don't see songs"
    assert REC in req_db.active_tracks()
    again = client.post("/v1/requests", headers=bearer(login(client)), json={
        "kind": "track", "musicBrainzRecordingId": REC, "musicBrainzReleaseGroupId": RG})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_requested"


def test_found_downloaded_filed_and_available(client, setup, monkeypatch):
    store, dl, tagged = setup
    make_user()
    rid = _request(client)["id"]
    enqueued, state = [], {"t": {"state": "InProgress", "percentComplete": 40.0, "id": "t1"}}

    async def search(text, timeout_s=20.0):
        assert text == "Peach Pit Drop the Guillotine"
        return [_hit()]

    async def enqueue(c):
        enqueued.append(c)

    async def transfer(c):
        return state["t"]
    monkeypatch.setattr(soulseek, "search", search)
    monkeypatch.setattr(soulseek, "enqueue", enqueue)
    monkeypatch.setattr(soulseek, "transfer", transfer)

    asyncio.run(tracks.tick())
    row = req_db.get(rid)
    assert row["status"] == "downloading" and enqueued[0].username == "alice"
    asyncio.run(tracks.tick())
    assert req_db.get(rid)["fetch"]["percent"] == 40.0
    assert tracks.pipeline(req_db.get(rid))["percent"] == 40.0

    (dl / "Peach Pit - Being So Normal (2017)").mkdir()
    (dl / "Peach Pit - Being So Normal (2017)" / "02 Drop the Guillotine.flac").write_bytes(b"audio")
    state["t"] = {"state": "Completed, Succeeded", "id": "t1"}
    asyncio.run(tracks.tick())
    row = req_db.get(rid)
    assert row["status"] == "scanning" and row["file_path"] == "_singles/Peach Pit/Being So Normal/02 Drop the Guillotine.flac"
    assert (store / "_singles" / "Peach Pit" / "Being So Normal" / "02 Drop the Guillotine.flac").read_bytes() == b"audio"
    assert tagged[0]["tracknumber"] == 2 and tagged[0]["musicbrainz_trackid"] == REC
    assert tagged[0]["album"] == "Being So Normal" and tagged[0]["musicbrainz_releasegroupid"] == RG

    async def refresh():
        lib_db.upsert_many([{"navidrome_id": "nd-bsn", "name": "Being So Normal", "artist": "Peach Pit",
                             "folder": "_singles/Peach Pit/Being So Normal", "norm_key": "k", "library_id": 1}])
        return {}

    async def paths(album_id, limit=1):
        return ["_singles/Peach Pit/Being So Normal/02 Drop the Guillotine.flac"]
    from continuo.workers import index
    monkeypatch.setattr(index, "refresh", refresh)
    monkeypatch.setattr(navidrome, "album_song_paths", paths)
    asyncio.run(tracks.tick())
    row = req_db.get(rid)
    assert row["status"] == "available" and row["navidrome_album_id"] == "nd-bsn"


def test_songs_go_to_the_singles_folder_lidarr_never_sees(setup):
    lib_db.upsert_many([{"navidrome_id": "nd-bsn", "name": "Being So Normal", "artist": "Peach Pit",
                         "mbz_release_group_id": RG, "folder": "Peach Pit/Being So Normal (2017)", "norm_key": "k"}])
    row = {"album_release_group_id": RG, "track": {"albumArtist": "Peach Pit", "albumTitle": "Being So Normal"}}
    assert tracks.album_folder(row) == "_singles/Peach Pit/Being So Normal"


def test_nobody_has_it_backs_off_and_still_looks(client, setup, monkeypatch):
    make_user()
    rid = _request(client)["id"]

    async def search(text, timeout_s=20.0):
        return [_hit(name="02 Drop the Guillotine (Live).flac")]   # a live version isn't the song
    monkeypatch.setattr(soulseek, "search", search)
    asyncio.run(tracks.tick())
    row = req_db.get(rid)
    assert row["status"] == "searching" and row["search_cycles"] == 1 and not row["still_looking"]
    asyncio.run(tracks.tick())
    assert req_db.get(rid)["search_cycles"] == 1, "not due again yet"
    req_db.update(rid, next_attempt_at=None)
    asyncio.run(tracks.tick())
    row = req_db.get(rid)
    assert row["search_cycles"] == 2 and row["still_looking"]
    assert "request the album instead" in tracks.pipeline(row)["detail"]
    tracks.search_now(row)
    assert req_db.get(rid)["search_cycles"] == 0


def test_a_failed_copy_is_skipped_for_the_next(client, setup, monkeypatch):
    make_user()
    rid = _request(client)["id"]

    async def search(text, timeout_s=20.0):
        return [_hit("alice"), {**_hit("bob"), "hasFreeUploadSlot": False}]

    async def enqueue(c):
        pass

    async def transfer(c):
        return {"state": "Completed, Errored", "id": "t"} if c.username == "alice" else {"state": "Queued", "id": "t2"}
    monkeypatch.setattr(soulseek, "search", search)
    monkeypatch.setattr(soulseek, "enqueue", enqueue)
    monkeypatch.setattr(soulseek, "transfer", transfer)
    asyncio.run(tracks.tick())
    assert req_db.get(rid)["fetch"]["username"] == "alice"
    asyncio.run(tracks.tick())   # alice's copy failed: back to searching, at once
    row = req_db.get(rid)
    assert row["status"] == "searching" and row["fetch"]["tried"][0][0] == "alice"
    asyncio.run(tracks.tick())
    assert req_db.get(rid)["fetch"]["username"] == "bob"


def test_file_names_follow_the_library():
    assert tracks.file_name({"title": "Drop the Guillotine", "position": 2}, "flac") == "02 Drop the Guillotine.flac"
    assert tracks.file_name({"title": "a/b", "position": 3, "disc": 2, "discs": 2}, "mp3") == "2-03 a_b.mp3"


def test_the_server_offers_song_requests(client, setup):
    make_user()
    assert "track_requests" in client.get("/v1/me", headers=bearer(login(client))).json()["capabilities"]


def test_a_busy_musicbrainz_is_retried(fake, monkeypatch):
    """MusicBrainz sheds load with 503s ("x-ratelimit-zone: global"); a lookup tries again."""
    import httpx
    calls = []

    def answer(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"title": "Drop the Guillotine", "length": 230000,
                                         "artist-credit": [{"name": "Peach Pit", "artist": {"id": "pp"}}]})
    monkeypatch.setattr(musicbrainz, "_RETRY_WAITS", (0.0, 0.0))
    fake.on("GET", f"https://musicbrainz.org/ws/2/recording/{REC}", handler=answer)
    rec = asyncio.run(musicbrainz.recording(REC))
    assert rec["title"] == "Drop the Guillotine" and len(calls) == 3


def test_an_unknown_song_is_not_found_but_a_busy_musicbrainz_is_not(fake, monkeypatch):
    import httpx
    monkeypatch.setattr(musicbrainz, "_RETRY_WAITS", (0.0, 0.0))
    fake.on("GET", f"https://musicbrainz.org/ws/2/recording/{REC}", handler=lambda req: httpx.Response(404))
    assert asyncio.run(musicbrainz.recording(REC)) is None
    fake.on("GET", f"https://musicbrainz.org/ws/2/recording/{REC}", handler=lambda req: httpx.Response(503))
    with pytest.raises(musicbrainz.Unavailable):
        asyncio.run(musicbrainz.recording(REC))


def test_a_song_requested_while_musicbrainz_is_busy_starts_once_it_answers(client, setup, monkeypatch):
    make_user()

    async def busy(mbid):
        raise musicbrainz.Unavailable("503")
    monkeypatch.setattr(musicbrainz, "recording", busy)
    musicbrainz._search_cache[("peach pit", 25)] = (10**12, [{"musicBrainzRecordingId": REC, "title": "Drop the Guillotine",
                                                              "artist": "Peach Pit", "album": {"year": 2017}}])
    try:
        out = _request(client)
    finally:
        musicbrainz._search_cache.clear()
    assert out["title"] == "Drop the Guillotine", "the title from the search it was found in"
    assert "MusicBrainz" in out["pipeline"]["detail"]
    row = req_db.get(out["id"])
    assert row["track"] is None and row["status"] == "pending"

    asyncio.run(tracks.tick())                        # still busy: later
    assert req_db.get(out["id"])["track"] is None and req_db.get(out["id"])["next_attempt_at"]
    monkeypatch.setattr(musicbrainz, "recording", lambda mbid: _rec())   # MusicBrainz answers again
    req_db.update(out["id"], next_attempt_at=None)

    async def search(text, timeout_s=20.0):
        return []
    monkeypatch.setattr(soulseek, "search", search)
    asyncio.run(tracks.tick())                        # MusicBrainz answers: details, then searching
    row = req_db.get(out["id"])
    assert row["track"]["albumTitle"] == "Being So Normal" and row["track"]["position"] == 2


async def _rec():
    return {"title": "Drop the Guillotine", "artist": "Peach Pit", "artistId": "pp", "durationMs": 230000}


def test_someone_who_rejected_us_isnt_asked_again(client, setup, monkeypatch):
    """stavriani rejected Visit Croatia, then was asked for their second copy and rejected again."""
    make_user()
    rid = _request(client)["id"]

    async def search(text, timeout_s=20.0):
        return [{**_hit("stavriani"), "files": _hit("stavriani")["files"] + _hit("stavriani", name="02 Drop the Guillotine (2).flac")["files"]},
                {**_hit("jagodka"), "hasFreeUploadSlot": False}]

    async def enqueue(c):
        pass

    async def transfer(c):
        return {"state": "Completed, Rejected", "id": "t"} if c.username == "stavriani" else {"state": "Queued", "id": "t2"}
    monkeypatch.setattr(soulseek, "search", search)
    monkeypatch.setattr(soulseek, "enqueue", enqueue)
    monkeypatch.setattr(soulseek, "transfer", transfer)
    asyncio.run(tracks.tick())
    assert req_db.get(rid)["fetch"]["username"] == "stavriani"
    asyncio.run(tracks.tick())                         # rejected
    assert req_db.get(rid)["fetch"]["refused"] == ["stavriani"]
    asyncio.run(tracks.tick())
    assert req_db.get(rid)["fetch"]["username"] == "jagodka", "not stavriani's other copy"
