# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""Request one album: ensure its artist is in Lidarr (nothing monitored, monitorNewItems=none),
monitor exactly that album, search. Never touches any other album of the artist."""
import asyncio
import logging

from .. import config, events
from ..db import requests as req_db
from ..db.conn import now
from ..notify import dispatch
from ..upstream import lidarr, soularr
from . import view

log = logging.getLogger("continuo")

_COMPILATION_HINT = ("Couldn't find this album in Lidarr to download — it may be a compilation "
                     "or have no matching release.")

# Background add tasks, kept referenced so they aren't garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()


async def request_album(foreign_album_id: str, request_id: int | None = None) -> dict:
    """Returns {lidarr_artist_id, lidarr_album_id, lidarr_release_id, title, artist, imageUrl}."""
    def event(type: str, detail: dict | None = None) -> None:
        if request_id is not None:
            req_db.add_event(request_id, type, detail)

    info = await lidarr.lookup_album_by_id(foreign_album_id)
    if not info:
        raise LookupError("This album isn't in Lidarr's metadata.")
    artist = info.get("artist", {}) or {}

    existing = await lidarr.find_library_artist(artist.get("foreignArtistId"))
    if existing:
        artist_id = existing["id"]
        # Lidarr only wants albums of *monitored* artists. A plain PUT: per-album monitoring is
        # untouched, monitorNewItems stays none.
        await lidarr.set_artist_monitored(artist_id, True)
        event("artist_monitored", {"lidarrArtistId": artist_id})
    else:
        created = await lidarr.add_artist(artist)
        artist_id = created["id"]
        event("artist_added", {"lidarrArtistId": artist_id, "monitorNewItems": "none"})

    album = await lidarr.wait_for_album(artist_id, foreign_album_id)
    if not album:
        raise RuntimeError(_COMPILATION_HINT)
    album_id = album["id"]

    # Metadata-profile changes can leave album rows with no releases/statistics; a monitor then
    # never builds a track list. Refresh once, blocking, and re-read.
    if not album.get("releases") or album.get("statistics") is None:
        event("waiting_for_metadata", {"lidarrAlbumId": album_id})
        await lidarr.refresh_artist(artist_id)
        album = await lidarr.get_album(album_id, fresh=True)

    if not await lidarr.monitor_album(artist_id, album_id):
        raise RuntimeError("Couldn't get Lidarr to monitor this album (its tracks never loaded). "
                           "Retry to try again.")
    if not existing:
        # Lidarr 3 adds an artist with addOptions.monitor=none as *unmonitored*, whatever
        # `monitored` says, and only monitored artists' albums are wanted (what Soularr reads).
        await lidarr.set_artist_monitored(artist_id, True)
    album = await lidarr.get_album(album_id, fresh=True)
    release_id = lidarr.monitored_release_id(album)
    event("album_monitored", {"lidarrAlbumId": album_id, "releaseId": release_id})

    if config.settings.trigger_album_search:
        await lidarr.album_search([album_id])
        event("search_fired", {"by": "request"})

    return {
        "lidarr_artist_id": artist_id,
        "lidarr_album_id": album_id,
        "lidarr_release_id": release_id,
        "title": info.get("title"),
        "artist": artist.get("artistName"),
        "imageUrl": lidarr.normalize_album(info)["imageUrl"],
    }


def broadcast(row: dict, pipeline: dict | None = None) -> None:
    events.publish("request.updated", view.slim(row, pipeline), row.get("user_id"))


async def process_request(request_id: int) -> None:
    """The add task behind POST /v1/requests (and retry, and the startup requeue)."""
    row = req_db.get(request_id)
    if not row:
        return
    try:
        res = await request_album(row["mb_release_group_id"], request_id)
    except Exception as e:  # noqa: BLE001 — every failure lands on the row, retryable
        log.exception("request %s: add failed", request_id)
        message = str(e) or e.__class__.__name__
        if not isinstance(e, (LookupError, RuntimeError)):
            message = "Lidarr couldn't be reached to add this album. Retry to try again."
        row = req_db.update(request_id, status="error", error=message)
        req_db.add_event(request_id, "error", {"message": message})
        broadcast(row)
        await dispatch.request_event(row, "request_error", message)
        return
    ts = now()
    row = req_db.update(
        request_id, lidarr_artist_id=res["lidarr_artist_id"],
        lidarr_album_id=res["lidarr_album_id"], lidarr_release_id=res["lidarr_release_id"],
        title=res["title"] or row["title"], artist=res["artist"] or row["artist"],
        artwork_url=res["imageUrl"] or row["artwork_url"], status="searching", error=None,
        last_search_at=ts if config.settings.trigger_album_search else row.get("last_search_at"),
    )
    req_db.add_event(request_id, "status", {"from": "pending", "to": "searching"})
    broadcast(row)
    # Soulseek joins the race at once rather than at Soularr's next cycle.
    soularr.trigger_soon(f"request {request_id}")


def dispatch_process(request_id: int) -> None:
    task = asyncio.get_running_loop().create_task(process_request(request_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def requeue_stranded() -> int:
    """A request whose add was interrupted by a restart sits 'pending' with nothing in Lidarr
    forever. Re-dispatch each."""
    stranded = req_db.pending_unprocessed()
    for row in stranded:
        dispatch_process(row["id"])
    if stranded:
        log.info("requeued %d request(s) stranded by a restart", len(stranded))
    return len(stranded)


async def retry(row: dict) -> str:
    """Un-stick a request: clear Soularr's denylist entry, re-monitor, re-search, kick Soularr.
    A request that never reached Lidarr re-runs the whole add."""
    rid = row["id"]
    soularr.clear_denylist_entry(row.get("lidarr_album_id"))
    req_db.add_event(rid, "retry", None)
    if not row.get("lidarr_album_id"):
        req_db.update(rid, status="pending", error=None)
        dispatch_process(rid)
        return "Retrying — adding it to Lidarr again."
    await lidarr.set_artist_monitored(row["lidarr_artist_id"], True)
    await lidarr.monitor_album(row["lidarr_artist_id"], row["lidarr_album_id"])
    await lidarr.album_search([row["lidarr_album_id"]])
    fields = {"search_cycles": 0, "still_looking": False, "last_search_at": now()}
    if row["status"] in ("error", "needs_attention"):
        fields.update(status="searching", error=None, attention=None)
    updated = req_db.update(rid, **fields)
    req_db.add_event(rid, "search_fired", {"by": "retry"})
    await soularr.try_trigger_run(f"retry {rid}")
    broadcast(updated)
    return "Retrying — searching again now."


async def search_now(row: dict) -> str:
    rid = row["id"]
    if row.get("lidarr_album_id"):
        await lidarr.album_search([row["lidarr_album_id"]])
        req_db.update(rid, last_search_at=now())
        req_db.add_event(rid, "search_fired", {"by": "search_now"})
    kicked = await soularr.try_trigger_run(f"search-now {rid}")
    if not row.get("lidarr_album_id") and not kicked:
        return "This request hasn't reached Lidarr yet."
    return "Searching now — this can take a minute."


async def search_all_now() -> int:
    ids = [r["lidarr_album_id"] for r in req_db.active()
           if r.get("lidarr_album_id") and r["status"] in ("searching", "pending")]
    if ids:
        await lidarr.album_search(ids)
        ts = now()
        for r in req_db.active():
            if r.get("lidarr_album_id") in ids:
                req_db.update(r["id"], last_search_at=ts)
                req_db.add_event(r["id"], "search_fired", {"by": "search_all"})
    await soularr.try_trigger_run("search-now (all)")
    return len(ids)
