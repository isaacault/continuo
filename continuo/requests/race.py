"""Racing slow downloads (#31): when a request's download looks slow, continuo starts Soulseek on
the same album without stopping the first. The first copy Lidarr imports wins and the rest are
cancelled, so a late finisher can't come in as an "upgrade".

Slow is judged from the first minutes, which predict far better than anything known up front:
after RACE_GRACE_MIN of downloading, the speed seen so far won't finish within RACE_SLOW_MIN, or
no bytes arrived for RACE_IDLE_MIN. Soularr can't race: it skips albums already in Lidarr's
queue. So continuo searches slskd itself, enqueues the best folder, and when it's complete has
Lidarr import it from the downloads folder, the path Soularr's downloads take."""
import logging
import os
import re
import shutil
import time
from typing import Optional

import httpx

from .. import config
from ..db import races as races_db
from ..db import requests as req_db
from ..db.conn import seconds_since
from ..fetch import soulseek
from ..upstream import lidarr, slskd

log = logging.getLogger("continuo")

# Lidarr queue id -> (first seen, sizeLeft then, last change, sizeLeft at last change)
_seen: dict[int, tuple[float, int, float, int]] = {}
MAX_ATTEMPTS = 2
IMPORT_GIVE_UP_S = 2 * 3600
# An album Lidarr has found nothing for: Soulseek is asked once it's been searching this long (Lidarr
# searches its indexers the moment a request arrives, and grabs within a minute or two when it can),
# and again a day after an attempt that failed (MAX_ATTEMPTS in all), one album per tick.
UNFOUND_AFTER_S = 10 * 60
UNFOUND_RETRY_S = 24 * 3600


def is_slow(first_t: float, first_left: int, last_t: float, left: int, now: float) -> bool:
    """Whether a download is slow enough to race, from its first and latest samples."""
    s = config.settings
    if now - first_t < s.race_grace_min * 60:
        return False
    if now - last_t >= s.race_idle_min * 60:
        return True
    done = first_left - left
    if done <= 0:
        return True
    speed = done / (now - first_t)
    return left / speed > s.race_slow_min * 60


def observe(q: dict, now: Optional[float] = None) -> bool:
    """Record a queue item's progress; True when it's slow."""
    qid, left = q.get("id"), q.get("sizeLeft")
    if qid is None or left is None:
        return False
    now = time.monotonic() if now is None else now
    prev = _seen.get(qid)
    if prev is None:
        _seen[qid] = (now, left, now, left)
        return False
    first_t, first_left, last_t, last_left = prev
    if left != last_left:
        last_t, last_left = now, left
        _seen[qid] = (first_t, first_left, last_t, last_left)
    return is_slow(first_t, first_left, last_t, last_left, now)


def forget(present_ids: set[int]) -> None:
    for qid in [q for q in _seen if q not in present_ids]:
        _seen.pop(qid, None)


async def start(row: dict) -> Optional[dict]:
    """Start Soulseek racing this request's album; the race row, or None when there's nothing to
    race with (recorded as failed, so the next tick doesn't search again at once)."""
    try:
        tracks = await lidarr.album_tracks(row["lidarr_album_id"])
    except httpx.HTTPError:
        tracks = []
    titles = [t.get("title") or "" for t in tracks]
    try:
        responses = await soulseek.search_any(soulseek.queries(row.get("artist") or "", row.get("title") or ""))
    except soulseek.Offline:
        log.info("race %s: Soulseek is offline, nothing raced for now", row["id"])
        return None     # not recorded as failed: a later tick tries again
    best = soulseek.best_album(responses, row.get("title") or "", titles)
    if best is None:
        races_db.add(row["id"], "soulseek", state="failed", detail="No complete copy on Soulseek")
        log.info("race %s: nothing on Soulseek to race with", row["id"])
        return None
    try:
        await soulseek.enqueue_album(best)
    except httpx.HTTPError as e:
        races_db.add(row["id"], "soulseek", username=best.username, state="failed", detail=f"slskd refused: {e}")
        return None
    race = races_db.add(row["id"], "soulseek", username=best.username, remote_folder=best.folder,
                        local_folder=best.local_folder,
                        files=[{"filename": f.filename, "size": f.size} for f in best.files])
    req_db.add_event(row["id"], "race_started", {"source": "soulseek", "user": best.username,
                                                 "files": len(best.files)})
    log.info("race %s: Soulseek (%s, %d files) racing a slow download", row["id"], best.username, len(best.files))
    return race


async def _follow(race: dict) -> None:
    """A Soulseek racer: all done -> Lidarr imports it; one failed for good -> the race is lost."""
    transfers = await slskd.transfers(race["username"])
    if transfers is None:
        return
    wanted = {f["filename"] for f in race["files"]}
    mine = [t for t in transfers if t.get("filename") in wanted]
    states = [soulseek.finished(t) for t in mine]
    if len(mine) < len(wanted) and _all_on_disk(race):
        pass    # Soularr clears finished transfers from slskd every cycle: the files are the word
    elif len(mine) < len(wanted) or any(s is None for s in states):
        return
    elif not all(states):
        races_db.finish(race["id"], "failed", "A Soulseek transfer failed")
        req_db.add_event(race["request_id"], "race_failed", {"source": "soulseek"})
        return
    await _import(race)


def _album_folder(race: dict) -> Optional[str]:
    """'Alaskan Tapes - We All Speak in Poems': Lidarr reads the artist from the folder's name and
    skips one it can't ("2016 - We All Speak In Poems" was an Unknown Artist), so the race names
    the folder the way Soularr does. None without the request's artist and title."""
    row = req_db.get(race["request_id"]) or {}
    if not row.get("artist") or not row.get("title"):
        return None
    name = f"{row['artist']} - {row['title']}"
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip(" .") or None


def _renamed(race: dict) -> str:
    """The race's folder, renamed to [_album_folder] when it's in slskd's downloads folder."""
    base, current = config.settings.slskd_downloads_dir, race["local_folder"]
    target = _album_folder(race)
    if not base or not target or target == current or not os.path.isdir(os.path.join(base, current)):
        return current
    if os.path.exists(os.path.join(base, target)):
        target = f"{target} ({race['id']})"
    try:
        os.rename(os.path.join(base, current), os.path.join(base, target))
    except OSError as e:
        log.warning("race %s: couldn't rename %s: %s", race["request_id"], current, e)
        return current
    races_db.set_folder(race["id"], target)
    return target


async def _import(race: dict, folder: Optional[str] = None) -> None:
    folder = folder or _renamed(race)
    path = f"{config.settings.lidarr_soulseek_path.rstrip('/')}/{folder}"
    try:
        await lidarr.import_folder(path)
    except httpx.HTTPError as e:
        log.warning("race %s: Lidarr import of %s failed: %s", race["request_id"], path, e)
        return
    races_db.set_state(race["id"], "importing", f"Lidarr importing {folder}")


def _import_skipped(race: dict) -> bool:
    """Handed to Lidarr under a name it couldn't read (before folders were renamed): the files are
    still there under the old name."""
    base = config.settings.slskd_downloads_dir
    target = _album_folder(race)
    return bool(base and target and race["local_folder"] != target and not race["local_folder"].startswith(target)
                and os.path.isdir(os.path.join(base, race["local_folder"])))


def _all_on_disk(race: dict) -> bool:
    """Every file of the race in slskd's downloads folder, whole (slskd adds _<ticks> to a name
    that was taken)."""
    base = config.settings.slskd_downloads_dir
    if not base or not race.get("local_folder"):
        return False
    folder = os.path.join(base, race["local_folder"])
    try:
        present = {name: os.path.getsize(os.path.join(folder, name)) for name in os.listdir(folder)}
    except OSError:
        return False
    for f in race["files"]:
        name = re.split(r"[\\/]", f["filename"])[-1]
        stem, ext = os.path.splitext(name)
        sizes = [size for n, size in present.items() if n == name or (n.startswith(stem + "_") and n.endswith(ext))]
        if f.get("size") not in sizes:
            return False
    return True


async def tick(rows: list[dict], queue_index: dict) -> None:
    """From the status loop: start races for slow downloads, and follow running ones."""
    s = config.settings
    if not s.race_enabled or not slskd.enabled():
        return
    forget({q["id"] for q in queue_index.values() if q.get("id") is not None})
    for row in rows:
        q = queue_index.get(row.get("lidarr_album_id"))
        if not q or q.get("protocol") != "torrent":
            continue
        if not observe(q):
            continue
        past = races_db.for_request(row["id"])
        if any(r["state"] in races_db.ACTIVE + ("won",) for r in past) or len(past) >= MAX_ATTEMPTS:
            continue
        try:
            await start(row)
        except Exception:  # noqa: BLE001 — one race must not stop the status loop
            log.exception("race %s: couldn't start", row["id"])
    for race in races_db.active():
        if race["state"] == "downloading":
            try:
                await _follow(race)
            except Exception:  # noqa: BLE001
                log.exception("race %s: follow failed", race["request_id"])
        elif _import_skipped(race):
            # Once, after renaming: a folder that can't be renamed isn't sent again every tick.
            folder = _renamed(race)
            if folder != race["local_folder"]:
                try:
                    await _import(race, folder)
                except Exception:  # noqa: BLE001
                    log.exception("race %s: re-import failed", race["request_id"])
        elif (seconds_since(race["started_at"]) or 0) > IMPORT_GIVE_UP_S:
            # Handed to Lidarr, but no import came (it rejected the folder): the race is over.
            races_db.finish(race["id"], "failed", "Lidarr didn't import the Soulseek copy")


def unfound_due(row: dict, past: list[dict]) -> bool:
    """Whether to try Soulseek for an album request nothing has been downloaded for."""
    if row.get("status") != "searching" or not row.get("lidarr_album_id"):
        return False
    if (seconds_since(row.get("created_at")) or 0) < UNFOUND_AFTER_S:
        return False
    if any(r["state"] in races_db.ACTIVE + ("won",) for r in past) or len(past) >= MAX_ATTEMPTS:
        return False
    last = max((r["started_at"] for r in past if r.get("started_at")), default=None)
    return last is None or (seconds_since(last) or 0) > UNFOUND_RETRY_S


async def unfound(rows: list[dict]) -> None:
    """From the status loop: an album request Lidarr's indexers have found nothing for, hours on,
    is fetched from Soulseek by continuo, as a race (Soularr passes over albums whose copies don't
    match the edition Lidarr monitors: We All Speak in Poems searched for 8 days with six full
    copies on Soulseek). One album per tick, so Soulseek isn't asked many things at once."""
    s = config.settings
    if not s.race_enabled or not slskd.enabled():
        return
    for row in rows:
        if not unfound_due(row, races_db.for_request(row["id"])):
            continue
        try:
            await start(row)
        except Exception:  # noqa: BLE001 — one race must not stop the status loop
            log.exception("race %s: couldn't start for an unfound album", row["id"])
        return


def _won_by_soulseek(payload: dict) -> bool:
    """Lidarr's import webhook names the download client for its own downloads; a folder
    imported by command (a Soulseek racer, or Soularr) has none."""
    return not (payload.get("downloadclient") or "").strip()


async def on_imported(rows: list[dict], payload: dict) -> None:
    """Lidarr imported the album: the winner stays, every other source is cancelled."""
    soulseek_won = _won_by_soulseek(payload)
    queue = None
    for row in rows:
        races = [r for r in races_db.for_request(row["id"]) if r["state"] in races_db.ACTIVE]
        if not races:
            continue
        if soulseek_won:
            for r in races:
                races_db.finish(r["id"], "won")
            queue = queue if queue is not None else await lidarr.queue_index()
            q = queue.get(row.get("lidarr_album_id"))
            if q and q.get("id") is not None:
                try:
                    await lidarr.remove_queue_item(q["id"], blocklist=False)
                    log.info("race %s: Soulseek won; cancelled the slow torrent", row["id"])
                except httpx.HTTPError as e:
                    log.warning("race %s: couldn't cancel the torrent: %s", row["id"], e)
            req_db.add_event(row["id"], "race_won", {"source": "soulseek"})
        else:
            for r in races:
                await cancel(r)
            req_db.add_event(row["id"], "race_won", {"source": "torrent"})


async def cancel(race: dict) -> None:
    """Stop a losing Soulseek racer and clear what it downloaded."""
    transfers = await slskd.transfers(race["username"]) or []
    wanted = {f["filename"] for f in race["files"]}
    for t in transfers:
        if t.get("filename") in wanted and t.get("id"):
            await slskd.cancel(race["username"], t["id"])
    base = config.settings.slskd_downloads_dir
    if base and race.get("local_folder"):
        folder = os.path.realpath(os.path.join(base, race["local_folder"]))
        if folder.startswith(os.path.realpath(base) + os.sep) and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    races_db.finish(race["id"], "lost", "The other download finished first")
    log.info("race %s: the torrent won; cancelled Soulseek (%s)", race["request_id"], race["username"])


def summary(request_id: int) -> Optional[dict]:
    """The running race for a request, for the pipeline line."""
    for r in races_db.for_request(request_id):
        if r["state"] in races_db.ACTIVE:
            return {"source": r["source"], "username": r["username"], "state": r["state"]}
    return None
