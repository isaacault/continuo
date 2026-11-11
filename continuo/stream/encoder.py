"""Encoding songs into HLS renditions with ffmpeg, and the disk cache they live in.

A rendition (song, kbit/s) is encoded once, from Navidrome's original file (`/rest/stream?
format=raw`, as the listening user), into <cache>/<song>/<kbps>/: `init.mp4` (continuo's gapless
rewrite of ffmpeg's `ff_init.mp4`), `0.m4s`, `1.m4s`, ... and `meta.json`.

Lossless sources (FLAC and the like) state their exact sample count, so the segment durations and
the tail padding are known before ffmpeg starts (gapless.plan): the init segment is written as
soon as the first segment exists and the player can start ~a second in, while later segments are
still being made (Opus encodes ~50x faster than real time here). Other sources are encoded in
full first and the numbers read from what ffmpeg wrote.

At most HLS_MAX_ENCODES ffmpeg processes run at once, at low CPU priority; songs encoded ahead of
the player (`prefetch`) take at most one of those slots, so the song being played never waits
behind them. The cache drops the least recently played songs once it passes HLS_CACHE_MB.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from .. import config
from ..upstream import navidrome
from . import gapless

log = logging.getLogger("continuo")

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOSSLESS = {"flac", "alac", "wavpack", "ape", "tta", "mlp", "truehd"}
READY_TIMEOUT_S = 90.0
SEGMENT_TIMEOUT_S = 90.0


class EncodeError(Exception):
    pass


@dataclass
class Source:
    codec: str
    rate: int
    channels: int
    samples: Optional[int]      # at `rate`; exact for lossless sources
    exact: bool


@dataclass
class Job:
    song: str
    kbps: int
    dir: Path
    prefetch: bool = False
    started: bool = False
    segments: tuple[float, ...] = ()
    error: Optional[str] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)    # init.mp4 + segment list known
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


_jobs: dict[tuple[str, int], Job] = {}
_slots: Optional[asyncio.Semaphore] = None
_ahead: Optional[asyncio.Semaphore] = None


def reset() -> None:
    """For tests: forget the jobs and the semaphores (bound to the previous event loop)."""
    global _slots, _ahead
    _jobs.clear()
    _slots = _ahead = None


def _semaphores() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    global _slots, _ahead
    if _slots is None:
        _slots = asyncio.Semaphore(config.settings.hls_max_encodes)
        _ahead = asyncio.Semaphore(max(1, config.settings.hls_max_encodes - 1))
    return _slots, _ahead


def valid_id(song: str) -> bool:
    return bool(_ID.match(song or ""))


def cache_root() -> Path:
    d = config.settings.hls_cache_dir or os.path.join(os.path.dirname(config.settings.db_path) or ".", "cache", "hls")
    return Path(d)


def song_dir(song: str) -> Path:
    if not valid_id(song):
        raise ValueError("bad song id")
    return cache_root() / song


def touch(song: str) -> None:
    """Mark a song as just played (the cache keeps the most recently played)."""
    d = song_dir(song)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".used").touch()


def check_source(song: str, fingerprint: str) -> None:
    """Drop a song's renditions when Navidrome's file changed (an upgrade replaces it in place)."""
    d = song_dir(song)
    marker = d / ".source"
    if marker.exists() and marker.read_text() != fingerprint:
        if not any(j.song == song and not j.done.is_set() for j in _jobs.values()):
            shutil.rmtree(d, ignore_errors=True)
            for key in [k for k in _jobs if k[0] == song]:
                _jobs.pop(key, None)
    d.mkdir(parents=True, exist_ok=True)
    marker.write_text(fingerprint)


def _source_url(song: str, as_user: Optional[str]) -> tuple[str, str]:
    params, headers = navidrome._subsonic_auth(as_user)
    params = {**params, "id": song, "format": "raw"}
    params.pop("f", None)
    url = f"{config.settings.navidrome_url}/rest/stream?{urlencode(params)}"
    return url, "".join(f"{k}: {v}\r\n" for k, v in headers.items())


async def _exec(cmd: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def probe(url: str, headers: str) -> Source:
    cmd = [config.settings.ffprobe_path, "-v", "error", "-select_streams", "a:0", "-show_streams",
           "-of", "json"]
    if headers:
        cmd += ["-headers", headers]
    rc, out, err = await _exec(cmd + [url])
    if rc != 0:
        raise EncodeError(f"ffprobe: {err.decode(errors='replace')[-300:].strip()}")
    streams = json.loads(out or b"{}").get("streams") or []
    if not streams:
        raise EncodeError("no audio stream")
    s = streams[0]
    codec = (s.get("codec_name") or "").lower()
    rate = int(s.get("sample_rate") or 0)
    channels = int(s.get("channels") or 2)
    samples = None
    tb = s.get("time_base") or ""
    if s.get("duration_ts") is not None and rate and tb == f"1/{rate}":
        samples = int(s["duration_ts"])
    elif s.get("duration") and rate:
        samples = round(float(s["duration"]) * rate)
    exact = samples is not None and (codec in _LOSSLESS or codec.startswith("pcm_"))
    return Source(codec, rate, channels, samples, exact)


def _ffmpeg_cmd(url: str, headers: str, kbps: int, channels: int, out: Path) -> list[str]:
    cmd = [config.settings.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", url, "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libopus", "-b:a", f"{kbps}k"]
    if channels > 2:
        cmd += ["-ac", "2"]
    cmd += ["-f", "hls", "-hls_time", str(gapless.SEGMENT_SECONDS), "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_flags", "temp_file",
            "-hls_fmp4_init_filename", "ff_init.mp4",
            "-hls_segment_filename", str(out / "%d.m4s"), str(out / "ff.m3u8")]
    return cmd


def _write(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, path)


def _ffmpeg_segments(m3u8: Path) -> tuple[float, ...]:
    return tuple(float(m) for m in re.findall(r"#EXTINF:([0-9.]+),", m3u8.read_text()))


def _actual(out: Path) -> tuple[int, int]:
    """(packets, declared samples) over the segments ffmpeg wrote."""
    packets = declared = 0
    i = 0
    while (out / f"{i}.m4s").exists():
        p, d = gapless.segment_samples((out / f"{i}.m4s").read_bytes())
        packets += p
        declared += d
        i += 1
    return packets, declared


def _from_disk(job: Job) -> bool:
    meta = job.dir / "meta.json"
    if not meta.exists() or not (job.dir / "init.mp4").exists():
        return False
    try:
        m = json.loads(meta.read_text())
    except ValueError:
        return False
    if not m.get("complete"):
        return False
    job.segments = tuple(m["segments"])
    job.started = True
    job.ready.set()
    job.done.set()
    return True


def ensure(song: str, kbps: int, as_user: Optional[str], *, prefetch: bool = False) -> Job:
    """The job for this rendition: finished on disk, running, or started now."""
    key = (song, kbps)
    job = _jobs.get(key)
    if job is not None and not prefetch and job.prefetch and not job.started and job.task:
        # Queued ahead and now wanted for playing: don't wait behind the other songs ahead.
        job.task.cancel()
        job = None
    if job is not None and not (job.done.is_set() and job.error):
        return job
    job = Job(song, kbps, song_dir(song) / str(kbps), prefetch=prefetch)
    _jobs[key] = job
    if not _from_disk(job):
        job.task = asyncio.get_running_loop().create_task(_run(job, as_user))
    return job


def cancel_waiting(song: str, kbps: int) -> None:
    """Drop a prefetch that hasn't started (the queue changed)."""
    job = _jobs.get((song, kbps))
    if job and job.prefetch and not job.started and job.task:
        job.task.cancel()
        _jobs.pop((song, kbps), None)


async def _run(job: Job, as_user: Optional[str]) -> None:
    slots, ahead = _semaphores()
    try:
        if job.prefetch:
            async with ahead:
                async with slots:
                    await _encode(job, as_user)
        else:
            async with slots:
                await _encode(job, as_user)
    except asyncio.CancelledError:
        job.error = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001 — reported to the waiting request
        job.error = str(e) or e.__class__.__name__
        log.warning("hls: %s at %d kbit/s failed: %s", job.song, job.kbps, job.error)
    finally:
        job.ready.set()
        job.done.set()
        if job.error:
            # A job cancelled before it started never made its directory, and a replacement
            # (the same song wanted for playing) may be using it now.
            if job.started and _jobs.get((job.song, job.kbps)) in (job, None):
                shutil.rmtree(job.dir, ignore_errors=True)
        else:
            evict()


async def _encode(job: Job, as_user: Optional[str]) -> None:
    job.started = True
    started = time.monotonic()
    shutil.rmtree(job.dir, ignore_errors=True)
    job.dir.mkdir(parents=True)
    url, headers = _source_url(job.song, as_user)
    src = await probe(url, headers)
    plan = None
    if src.exact and src.samples:
        plan = gapless.plan(gapless.resampled_count(src.samples, src.rate))
        job.segments = plan.segments
    run = asyncio.get_running_loop().create_task(
        _exec(_ffmpeg_cmd(url, headers, job.kbps, src.channels, job.dir)))
    if plan is not None:
        # Serve as soon as the first segment is whole (temp_file: it appears under its own name
        # only when complete, and by then ffmpeg has written the init segment).
        while not (job.dir / "0.m4s").exists() and not run.done():
            await asyncio.sleep(0.05)
        if (job.dir / "ff_init.mp4").exists():
            init = (job.dir / "ff_init.mp4").read_bytes()
            _write(job.dir / "init.mp4",
                   gapless.rewrite_init(init, plan.preskip, plan.padding, plan.samples))
            job.ready.set()
    rc, _out, err = await run
    if rc != 0:
        raise EncodeError(f"ffmpeg exited {rc}: {err.decode(errors='replace')[-300:].strip()}")
    packets, declared = _actual(job.dir)
    init = (job.dir / "ff_init.mp4").read_bytes()
    pre = gapless.preskip(init)
    if plan is None:
        samples = declared - pre
        _write(job.dir / "init.mp4",
               gapless.rewrite_init(init, pre, packets * gapless.OPUS_FRAME - declared, samples))
        job.segments = _ffmpeg_segments(job.dir / "ff.m3u8")
    elif (packets, declared) != (plan.packets, plan.preskip + plan.samples):
        log.warning("hls: %s at %d kbit/s: planned %d packets / %d samples, ffmpeg wrote %d / %d",
                    job.song, job.kbps, plan.packets, plan.preskip + plan.samples, packets, declared)
    _write(job.dir / "meta.json", json.dumps({
        "complete": True, "segments": list(job.segments), "codec": src.codec, "rate": src.rate,
        "exact": src.exact, "kbps": job.kbps}))
    job.ready.set()
    log.info("hls: %s at %d kbit/s encoded in %.1f s (%s, %d segments)", job.song, job.kbps,
             time.monotonic() - started, src.codec, len(job.segments))


async def wait_ready(job: Job, timeout: float = READY_TIMEOUT_S) -> Job:
    await asyncio.wait_for(job.ready.wait(), timeout)
    if job.error:
        raise EncodeError(job.error)
    return job


async def wait_file(job: Job, name: str, timeout: float = SEGMENT_TIMEOUT_S) -> Optional[Path]:
    """The file once ffmpeg has written it whole; None if the encode ended without it."""
    path = job.dir / name
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            return path
        if job.done.is_set():
            if job.error:
                raise EncodeError(job.error)
            return path if path.exists() else None
        if time.monotonic() > deadline:
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.05)


def _size(d: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def evict() -> None:
    """Drop least recently played songs until the cache is within HLS_CACHE_MB."""
    root = cache_root()
    if not root.exists():
        return
    cap = config.settings.hls_cache_mb * 1024 * 1024
    songs = []
    for d in root.iterdir():
        if d.is_dir():
            used = d / ".used"
            songs.append((used.stat().st_mtime if used.exists() else 0.0, d, _size(d)))
    total = sum(s for _, _, s in songs)
    busy = {j.song for j in _jobs.values() if not j.done.is_set()}
    for _, d, size in sorted(songs, key=lambda s: s[0]):
        if total <= cap:
            break
        if d.name in busy:
            continue
        shutil.rmtree(d, ignore_errors=True)
        for key in [k for k in _jobs if k[0] == d.name]:
            _jobs.pop(key, None)
        total -= size
