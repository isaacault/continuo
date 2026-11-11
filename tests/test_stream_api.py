"""Adaptive streaming API (polyphony#185) with a stand-in ffmpeg that writes real ffmpeg output.

The stand-in ffprobe/ffmpeg answer as ffmpeg 9.0.2 did for track 1 of the gapless spike (a FLAC
of 441037 samples at 44.1 kHz; tests/fixtures/hls), so the whole path runs: probe, plan, the
gapless init rewrite, the playlists, the segments.
"""
import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from conftest import bearer, login, make_user

from continuo import config
from continuo.stream import encoder

FIX = Path(__file__).parent / "fixtures" / "hls"
ND = "http://navidrome.test/rest"


class FakeFfmpeg:
    def __init__(self, codec="flac", samples=441037, fail=False):
        self.codec, self.samples, self.fail = codec, samples, fail
        self.encodes: list[list[str]] = []
        self.probes = 0

    async def __call__(self, cmd):
        if os.path.basename(cmd[0]) == "ffprobe" or "ffprobe" in cmd:
            self.probes += 1
            stream = {"codec_name": self.codec, "sample_rate": "44100", "channels": 2,
                      "time_base": "1/44100"}
            if self.codec == "flac":
                stream["duration_ts"] = self.samples
            else:
                stream["duration"] = f"{self.samples / 44100:.6f}"
            return 0, json.dumps({"streams": [stream]}).encode(), b""
        self.encodes.append(cmd)
        if self.fail:
            return 1, b"", b"Invalid data found when processing input"
        out = Path(cmd[-1]).parent
        shutil.copy(FIX / "ffmpeg_init.mp4", out / "ff_init.mp4")
        shutil.copy(FIX / "0.m4s", out / "0.m4s")
        await asyncio.sleep(0.05)
        shutil.copy(FIX / "1.m4s", out / "1.m4s")
        (out / "ff.m3u8").write_text("#EXTM3U\n#EXTINF:6.000000,\n0.m4s\n#EXTINF:4.007354,\n1.m4s\n")
        return 0, b"", b""


@pytest.fixture
def hls(fake, monkeypatch, tmp_path):
    env = dict(os.environ, HLS_CACHE_DIR=str(tmp_path / "hls"))
    monkeypatch.setattr(os, "environ", env)
    config.reload(env)
    encoder.reset()
    ff = FakeFfmpeg()
    monkeypatch.setattr(encoder, "_exec", ff)
    song = {"id": "tr1", "size": 2_000_000, "suffix": "flac", "duration": 10}
    fake.on("GET", f"{ND}/getSong",
            handler=lambda req: httpx.Response(200, json={"subsonic-response": (
                {"status": "ok", "song": song} if req.url.params.get("id") == "tr1" else
                {"status": "failed", "error": {"code": 70, "message": "not found"}})}))
    yield ff, song
    encoder.reset()


@pytest.fixture
def session(client):
    make_user()
    return login(client)


def test_needs_a_signed_in_device(client, hls):
    assert client.get("/v1/stream/tr1/master.m3u8").status_code == 401
    assert client.get("/v1/stream/tr1/64/0.m4s").status_code == 401


def test_master_lists_renditions_within_the_clients_cap(client, hls, session):
    r = client.get("/v1/stream/tr1/master.m3u8?max=128", headers=bearer(session))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "32/index.m3u8" in r.text and "128/index.m3u8" in r.text and "256/" not in r.text


def test_master_asks_navidrome_as_the_listener(client, fake, hls, session):
    client.get("/v1/stream/tr1/master.m3u8", headers=bearer(session))
    call = fake.called("GET", f"{ND}/getSong")[-1]
    assert call.headers["Remote-User"] == "isaac"


def test_unknown_song_is_404(client, hls, session):
    assert client.get("/v1/stream/nope/master.m3u8", headers=bearer(session)).status_code == 404
    assert client.get("/v1/stream/..%2Fx/master.m3u8", headers=bearer(session)).status_code == 404


def test_media_playlist_is_planned_from_the_source(client, hls, session):
    r = client.get("/v1/stream/tr1/64/index.m3u8", headers=bearer(session))
    assert r.status_code == 200
    assert "#EXTINF:6.000000,\n0.m4s\n#EXTINF:4.007354,\n1.m4s" in r.text
    assert r.text.rstrip().endswith("#EXT-X-ENDLIST")


def test_init_is_the_gapless_rewrite(client, hls, session):
    r = client.get("/v1/stream/tr1/64/init.mp4", headers=bearer(session))
    assert r.status_code == 200
    assert r.content == (FIX / "media3_verified_init.mp4").read_bytes()


def test_segments_are_served_and_encoded_once(client, hls, session):
    ff, _ = hls
    h = bearer(session)
    assert client.get("/v1/stream/tr1/64/0.m4s", headers=h).content == (FIX / "0.m4s").read_bytes()
    assert client.get("/v1/stream/tr1/64/1.m4s", headers=h).content == (FIX / "1.m4s").read_bytes()
    assert client.get("/v1/stream/tr1/64/2.m4s", headers=h).status_code == 404
    assert len(ff.encodes) == 1
    cmd = ff.encodes[0]
    assert cmd[cmd.index("-b:a") + 1] == "64k" and "libopus" in cmd and "temp_file" in cmd
    url = cmd[cmd.index("-i") + 1]
    assert url.startswith("http://navidrome.test/rest/stream?") and "format=raw" in url
    assert "id=tr1" in url and "Remote-User: isaac" in cmd[cmd.index("-headers") + 1]


def test_each_level_is_its_own_encode(client, hls, session):
    ff, _ = hls
    h = bearer(session)
    client.get("/v1/stream/tr1/32/0.m4s", headers=h)
    client.get("/v1/stream/tr1/256/0.m4s", headers=h)
    assert sorted(c[c.index("-b:a") + 1] for c in ff.encodes) == ["256k", "32k"]


def test_bad_level_or_file_is_404(client, hls, session):
    h = bearer(session)
    assert client.get("/v1/stream/tr1/100/index.m3u8", headers=h).status_code == 404
    assert client.get("/v1/stream/tr1/64/ff_init.mp4", headers=h).status_code == 404
    assert client.get("/v1/stream/tr1/64/meta.json", headers=h).status_code == 404


def test_a_finished_rendition_is_served_from_disk_after_a_restart(client, hls, session):
    ff, _ = hls
    h = bearer(session)
    client.get("/v1/stream/tr1/64/1.m4s", headers=h)
    encoder._jobs.clear()                       # a restart forgets the jobs, not the files
    r = client.get("/v1/stream/tr1/64/init.mp4", headers=h)
    assert r.content == (FIX / "media3_verified_init.mp4").read_bytes()
    assert len(ff.encodes) == 1


def test_a_changed_file_is_encoded_again(client, hls, session):
    ff, song = hls
    h = bearer(session)
    client.get("/v1/stream/tr1/master.m3u8", headers=h)
    client.get("/v1/stream/tr1/64/1.m4s", headers=h)
    song["size"] = 3_000_000                    # Lidarr upgraded the file in place
    client.get("/v1/stream/tr1/master.m3u8", headers=h)
    client.get("/v1/stream/tr1/64/1.m4s", headers=h)
    assert len(ff.encodes) == 2


def test_lossy_source_is_trimmed_from_what_ffmpeg_wrote(client, hls, session, monkeypatch):
    # An MP3's length isn't sample-exact up front: encode first, then read packets and samples.
    monkeypatch.setattr(encoder, "_exec", FakeFfmpeg(codec="mp3", samples=441037))
    h = bearer(session)
    r = client.get("/v1/stream/tr1/64/index.m3u8", headers=h)
    assert "#EXTINF:6.000000,\n0.m4s\n#EXTINF:4.007354,\n1.m4s" in r.text
    assert client.get("/v1/stream/tr1/64/init.mp4", headers=h).content == \
        (FIX / "media3_verified_init.mp4").read_bytes()


def test_a_failed_encode_is_502_and_retried_next_time(client, hls, session, monkeypatch):
    h = bearer(session)
    monkeypatch.setattr(encoder, "_exec", FakeFfmpeg(fail=True))
    assert client.get("/v1/stream/tr1/64/0.m4s", headers=h).status_code == 502
    monkeypatch.setattr(encoder, "_exec", FakeFfmpeg())
    assert client.get("/v1/stream/tr1/64/0.m4s", headers=h).status_code == 200


def test_upcoming_songs_are_encoded_ahead(client, hls, session):
    ff, _ = hls
    h = bearer(session)
    r = client.post("/v1/stream/upcoming", headers=h,
                    json={"songs": ["tr1", "tr2", "tr3", "tr4", "tr5", "tr6"], "kbps": 100,
                          "lowerToo": True})
    assert r.status_code == 200
    # the nearest rung at or below 100 is 64; the next song also one rung lower; four songs ahead
    assert r.json()["encoding"] == [{"song": "tr1", "kbps": 64}, {"song": "tr1", "kbps": 32},
                                    {"song": "tr2", "kbps": 64}, {"song": "tr3", "kbps": 64},
                                    {"song": "tr4", "kbps": 64}]
    jobs = [encoder._jobs[(s, k)] for s, k in [("tr1", 64), ("tr2", 64)]]
    assert all(j.prefetch for j in jobs)
    # once encoded ahead, playing it needs no new encode
    client.get("/v1/stream/tr1/64/1.m4s", headers=h)
    _settle()
    assert sum(1 for c in ff.encodes if "id=tr1" in c[c.index("-i") + 1]
               and c[c.index("-b:a") + 1] == "64k") == 1
    assert len(ff.encodes) == 5


def _settle(timeout: float = 10.0) -> None:
    """Wait for the encodes running in the app's event loop (TestClient's thread) to finish."""
    import time
    deadline = time.monotonic() + timeout
    while any(not j.done.is_set() for j in list(encoder._jobs.values())):
        assert time.monotonic() < deadline, "encodes still running"
        time.sleep(0.05)


def test_upcoming_ignores_bad_ids(client, hls, session):
    r = client.post("/v1/stream/upcoming", headers=bearer(session),
                    json={"songs": ["../etc", "tr1"], "kbps": 32})
    assert r.json()["encoding"] == [{"song": "tr1", "kbps": 32}]


def test_cache_drops_least_recently_played_songs(fake, monkeypatch, tmp_path):
    env = dict(os.environ, HLS_CACHE_DIR=str(tmp_path / "hls"), HLS_CACHE_MB="1")
    monkeypatch.setattr(os, "environ", env)
    config.reload(env)
    encoder.reset()
    for i, song in enumerate(["old", "mid", "new"]):
        d = encoder.song_dir(song) / "64"
        d.mkdir(parents=True)
        (d / "0.m4s").write_bytes(b"\0" * 600_000)
        encoder.touch(song)
        os.utime(encoder.song_dir(song) / ".used", (1000 + i, 1000 + i))
    encoder.evict()
    assert sorted(p.name for p in encoder.cache_root().iterdir()) == ["new"]
