"""Play now (#12): a song you don't have, heard within a minute.

start() picks the fastest acceptable Soulseek copy (the `fastest` policy: a free slot, a short
queue and speed before format), downloads it, and the app plays it while it downloads: audio_path()
is the growing file in slskd's incomplete folder, then the finished one, then its copy in the media
cache (origin 'stream', evicted least recently used). A copy that's rejected, vanishes or doesn't
start in START_TIMEOUT_S is dropped for the next, up to MAX_COPIES. keep() turns a stream into a
single-song request (#24) that files the cached copy, nothing downloaded again."""
import asyncio
import logging
import os
import shutil
import uuid
from typing import Optional

from .. import config
from ..db import cache as cache_db
from ..db import streams as streams_db
from ..db.conn import seconds_since
from ..fetch import soulseek
from ..library import cache

log = logging.getLogger("continuo")

# Bytes on disk before the app is told to start playing (a FLAC header and a few seconds of audio).
START_BYTES = 256 * 1024
START_TIMEOUT_S = 25.0
MAX_COPIES = 3
# Queued songs (polyphony#164) are fetched this many at a time per account, needed soonest first;
# a song tapped to play now starts at once whatever is running.
MAX_FETCHING = 2
SEARCH_QUIET_S = 4.0
SEARCH_MAX_S = 15.0
POLL_S = 1.0

_tasks: set[asyncio.Task] = set()


def start(user_id: Optional[int], recording_id: str, album_rg: Optional[str], title: str, artist: str,
          duration_ms: Optional[int], queued: bool = False) -> dict:
    """A stream for this song: an earlier one still in the cache plays at once; else a fetch, at
    once (played now) or [queued] behind the account's other queued songs (polyphony#164)."""
    done = streams_db.ready_for(recording_id)
    if done and audio_path(done):
        cache_db.touch(done["cache_item_id"])
        return done
    if queued:
        row = streams_db.create(str(uuid.uuid4()), user_id, recording_id, album_rg, title, artist, duration_ms,
                                status="queued")
        kick(user_id)
        return streams_db.get(row["id"])
    row = streams_db.create(str(uuid.uuid4()), user_id, recording_id, album_rg, title, artist, duration_ms,
                            priority=-1)
    _launch(row)
    return row


def _launch(row: dict) -> None:
    task = asyncio.get_running_loop().create_task(_run(row["id"]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(lambda _t, uid=row["user_id"]: kick(uid))


def kick(user_id: Optional[int]) -> None:
    """Start the account's queued fetches, soonest needed first, up to MAX_FETCHING at a time."""
    while streams_db.fetching_count(user_id) < MAX_FETCHING:
        row = streams_db.next_queued(user_id)
        if row is None:
            return
        _launch(streams_db.update(row["id"], status="searching"))


def prioritise(user_id: Optional[int], ordered_ids: list[str]) -> int:
    """The app's queue order of its Play now songs: the queued ones are fetched in that order."""
    n = streams_db.set_priorities(user_id, ordered_ids)
    kick(user_id)
    return n


def requeue_interrupted() -> int:
    """At start: a fetch a restart cut off goes back in its account's queue, and queues resume."""
    from ..db.conn import connect
    with connect() as c:
        n = c.execute(f"UPDATE streams SET status='queued' WHERE status IN ({','.join('?' * len(streams_db.FETCHING))})",
                      streams_db.FETCHING).rowcount
    for uid in streams_db.queued_user_ids():
        kick(uid)
    return n


def view(row: dict) -> dict:
    """The app's view of a stream."""
    path = audio_path(row)
    have = os.path.getsize(path) if path else 0
    size = row.get("size") or 0
    return {"id": row["id"], "status": row["status"], "title": row["title"], "artist": row["artist"],
            "durationMs": row.get("duration_ms"), "format": row.get("extension"), "bytes": have, "size": size or None,
            "percent": round(100 * have / size, 1) if size else 0, "playable": row["status"] in ("streaming", "ready"),
            "error": row.get("error"), "kept": bool((row.get("tried") or {}).get("kept"))}


def _candidate(row: dict) -> soulseek.Candidate:
    return soulseek.Candidate(username=row["username"], filename=row["filename"], size=row.get("size") or 0,
                              extension=row.get("extension") or "", bit_rate=None, length_s=None,
                              free_slot=False, queue_length=0, upload_speed=0)


def _partial_path(row: dict) -> Optional[str]:
    """The file slskd is still writing: <incomplete>/<user>/.../<file name>."""
    base = config.settings.slskd_incomplete_dir
    if not base or not row.get("username"):
        return None
    name = _candidate(row).basename
    root = os.path.join(base, row["username"])
    for dirpath, _dirs, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    return None


def _cached_path(row: dict) -> Optional[str]:
    item = cache_db.get(row["cache_item_id"]) if row.get("cache_item_id") else None
    if not item:
        return None
    try:
        path = os.path.join(cache.cache_dir(), item["cache_path"])
    except cache.Unavailable:
        return None
    return path if os.path.isfile(path) else None


def audio_path(row: dict) -> Optional[str]:
    """Where the stream's audio is right now: cached, finished in slskd's downloads, or still
    downloading. None before any byte arrived (or once the cache dropped it)."""
    if row["status"] == "ready":
        return _cached_path(row)
    if not row.get("username"):
        return None
    from .tracks import _downloaded_path
    return _downloaded_path(_candidate(row)) or _partial_path(row)


async def _run(stream_id: str) -> None:
    try:
        await _fetch(stream_id)
    except Exception as e:  # noqa: BLE001 — a stream fails, the server doesn't
        log.exception("play now %s failed", stream_id)
        streams_db.update(stream_id, status="failed", error=str(e) or e.__class__.__name__)


async def _fetch(stream_id: str) -> None:
    row = streams_db.get(stream_id)
    responses = await soulseek.search(f"{row['artist']} {row['title']}".strip(), quiet_s=SEARCH_QUIET_S,
                                      max_s=SEARCH_MAX_S)
    tried: set[tuple[str, str]] = set()
    refused: set[str] = set()
    for _ in range(MAX_COPIES):
        pool = [dict(r, files=[f for f in r.get("files") or [] if (r.get("username"), f.get("filename")) not in tried])
                for r in responses if r.get("username") not in refused]
        best = soulseek.best(pool, row["artist"], row["title"], row.get("duration_ms"), policy=soulseek.FASTEST,
                             variants=False)
        if best is None:
            break
        tried.add((best.username, best.filename))
        row = streams_db.update(stream_id, status="downloading", username=best.username, filename=best.filename,
                                size=best.size, extension=best.extension,
                                tried={"tried": sorted(tried), "refused": sorted(refused)})
        log.info("play now %s: %r from %s", stream_id, row["title"], best.username)
        try:
            await soulseek.enqueue(best)
        except Exception as e:  # noqa: BLE001 — refused by slskd: the next copy
            log.warning("play now %s: enqueue failed: %s", stream_id, e)
            continue
        outcome = await _follow(stream_id)
        if outcome == "ready":
            return
        if outcome == "rejected":
            refused.add(best.username)
    streams_db.update(stream_id, status="failed", error="Nobody on Soulseek could send this song right now.")


async def _follow(stream_id: str) -> str:
    """Follow one copy: 'ready' once it's in the cache, 'rejected' or 'failed' to try another."""
    row = streams_db.get(stream_id)
    c = _candidate(row)
    started_at = row["updated_at"]
    while True:
        await asyncio.sleep(POLL_S)
        t = await soulseek.transfer(c)
        done = soulseek.finished(t)
        if t is None or done is False:
            state = (t or {}).get("state") or "gone"
            log.info("play now %s: %s's copy %s", stream_id, c.username, state)
            await _cancel(c, t)
            return "rejected" if "Rejected" in state else "failed"
        path = audio_path(row)
        have = os.path.getsize(path) if path and os.path.exists(path) else 0
        if row["status"] == "downloading" and (have >= START_BYTES or done):
            row = streams_db.update(stream_id, status="streaming")
        if done:
            return "ready" if _to_cache(stream_id) else "failed"
        if row["status"] == "downloading" and (seconds_since(started_at) or 0) > START_TIMEOUT_S:
            log.info("play now %s: %s's copy didn't start in %.0f s", stream_id, c.username, START_TIMEOUT_S)
            await _cancel(c, t)
            return "failed"


async def _cancel(c: soulseek.Candidate, t: Optional[dict]) -> None:
    from ..upstream import slskd
    try:
        if t and t.get("id") and soulseek.finished(t) is None:
            await slskd.cancel(c.username, t["id"])
    except Exception:  # noqa: BLE001 — best effort
        pass


def _to_cache(stream_id: str) -> bool:
    """The finished file into the media cache (a move: same filesystem), so slskd's folder stays clean."""
    from .tracks import _downloaded_path
    row = streams_db.get(stream_id)
    src = _downloaded_path(_candidate(row))
    if not src:
        return False
    try:
        base = cache.cache_dir()
    except cache.Unavailable:
        return False
    rel = f"stream-{stream_id}/{os.path.basename(src)}"
    dest = os.path.join(base, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src, dest)
    try:
        os.rmdir(os.path.dirname(src))     # slskd's per-copy folder, now empty
    except OSError:
        pass
    item = cache_db.add({"kind": "track", "origin": "stream", "title": row["title"], "artist": row["artist"],
                         "mb_recording_id": row["recording_id"], "mb_release_group_id": row.get("album_rg"),
                         "cache_path": rel, "bytes": os.path.getsize(dest)})
    streams_db.update(stream_id, status="ready", cache_item_id=item["id"])
    cache.evict()
    return True


def keep(user: dict, stream_id: str) -> dict:
    """Keep a streamed song: a single-song request (#24) that files the cached copy into the
    account's library, MusicBrainz-tagged; nothing is downloaded again."""
    from ..db import requests as req_db
    row = streams_db.get(stream_id)
    if row is None or row["status"] != "ready" or not _cached_path(row):
        raise LookupError("That song hasn't finished downloading yet.")
    existing = req_db.by_release_group(row["recording_id"], kind="track")
    if existing and existing["status"] in req_db.ACTIVE + ("available",):
        return existing
    req = req_db.create(mb_release_group_id=row["recording_id"], title=row["title"], artist=row["artist"],
                        year=None, artwork_url=None, user_id=user["id"], kind="track")
    req = req_db.update(req["id"], album_release_group_id=row.get("album_rg"), track=None,
                        fetch={"local": _cached_path(row), "extension": row.get("extension")})
    req_db.add_event(req["id"], "requested", {"by": user["username"], "kind": "track", "from": "play_now"})
    streams_db.update(stream_id, tried={**(row.get("tried") or {}), "kept": True})
    return req
