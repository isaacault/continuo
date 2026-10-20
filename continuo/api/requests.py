"""Album requests (PLAN.md §3.3)."""
import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, Query

from ..auth.deps import CurrentUser, can_see, require_user
from ..db import library as lib_db
from ..db import requests as req_db
from ..db import users as users_db
from ..errors import ApiError, UpstreamError
from ..requests import orchestrate, pipeline, view
from ..upstream import lidarr
from .schemas import RequestIn

log = logging.getLogger("continuo")
router = APIRouter(prefix="/v1/requests", tags=["requests"])

# Bound the burst of Lidarr/slskd calls when a long request list resolves live pipelines.
_CONCURRENCY = 8


def _get_visible(rid: int, user: CurrentUser) -> dict:
    row = req_db.get(rid)
    if row is None or not can_see(user, row.get("user_id")):
        raise ApiError(404, "not_found", "Request not found.")
    return row


async def _resolve(rows: list[dict], user: CurrentUser) -> list[dict]:
    names = users_db.usernames_by_id()
    queue = await lidarr.queue_index() if any(
        r["status"] in req_db.ACTIVE for r in rows) else {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(row: dict) -> dict:
        async with sem:
            p = await pipeline.compute(row, queue)
        return view.to_out(row, p, names.get(row.get("user_id")))

    return await asyncio.gather(*(one(r) for r in rows))


@router.post("", status_code=201)
async def create_request(body: RequestIn, user: CurrentUser = Depends(require_user)) -> dict:
    rg = body.musicBrainzReleaseGroupId.lower()
    existing = req_db.by_release_group(rg)
    if existing and existing["status"] in req_db.ACTIVE:
        raise ApiError(409, "already_requested", "That album is already requested.",
                       requestId=existing["id"])
    in_lib = lib_db.by_release_group(rg)
    if in_lib:
        raise ApiError(409, "in_library", "That album is already in the library.",
                       navidromeAlbumId=in_lib[0]["navidrome_id"])
    try:
        info = await lidarr.lookup_album_by_id(rg)
    except httpx.HTTPError as e:
        raise UpstreamError("lidarr") from e
    if not info:
        raise ApiError(404, "not_found", "That album isn't in Lidarr's catalogue.")
    album = lidarr.normalize_album(info)
    if album["releaseIds"]:
        lib = lib_db.by_release_ids(album["releaseIds"])
        if lib:
            raise ApiError(409, "in_library", "That album is already in the library.",
                           navidromeAlbumId=lib[0]["navidrome_id"])
    row = req_db.create(mb_release_group_id=rg, title=album["title"], artist=album["artist"],
                        year=album["year"], artwork_url=album["imageUrl"], user_id=user.id,
                        note=body.note)
    req_db.add_event(row["id"], "requested", {"by": user.username, "kind": body.kind})
    orchestrate.dispatch_process(row["id"])
    log.info("request %s: %s — %s by %s", row["id"], album["title"], album["artist"], user.username)
    return view.to_out(row, pipeline.build("requested", detail="Adding to Lidarr…", row=row),
                       user.username)


@router.get("")
async def list_requests(scope: str = Query("all", pattern="^(active|all)$"),
                        mine: bool = Query(False), limit: int = Query(100, ge=1, le=500),
                        cursor: int | None = Query(None),
                        user: CurrentUser = Depends(require_user)) -> dict:
    only_user = user.id if (mine or not user.is_admin) else None
    statuses = req_db.ACTIVE if scope == "active" else None
    rows = req_db.list_requests(user_id=only_user, statuses=statuses, limit=limit + 1,
                                before_id=cursor)
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": await _resolve(rows, user),
            "cursor": rows[-1]["id"] if more and rows else None}


@router.post("/search-now")
async def search_all_now(_user: CurrentUser = Depends(require_user)) -> dict:
    try:
        n = await orchestrate.search_all_now()
    except httpx.HTTPError as e:
        raise UpstreamError("lidarr") from e
    return {"ok": True, "message": f"Searching now for {n} request{'s' if n != 1 else ''}."}


@router.post("/clear-available")
async def clear_available(user: CurrentUser = Depends(require_user)) -> dict:
    n = req_db.delete_available(user_id=None if user.is_admin else user.id)
    return {"ok": True, "message": f"Cleared {n} fulfilled request{'s' if n != 1 else ''}.",
            "count": n}


@router.get("/{rid}")
async def get_request(rid: int, user: CurrentUser = Depends(require_user)) -> dict:
    return (await _resolve([_get_visible(rid, user)], user))[0]


@router.get("/{rid}/events")
async def request_events(rid: int, user: CurrentUser = Depends(require_user)) -> list[dict]:
    _get_visible(rid, user)
    return req_db.events(rid)


@router.delete("/{rid}")
async def delete_request(rid: int, user: CurrentUser = Depends(require_user)) -> dict:
    """Remove a request and stop it being wanted (unmonitor the album). Files already downloaded
    are never touched."""
    row = _get_visible(rid, user)
    warn = ""
    if row.get("lidarr_album_id") and row["status"] != "available":
        try:
            await lidarr.set_album_monitored([row["lidarr_album_id"]], False)
        except httpx.HTTPError as e:
            log.warning("delete %s: couldn't unmonitor album: %s", rid, e)
            warn = " (couldn't reach Lidarr to unmonitor it — it may still be wanted)"
    req_db.delete(rid)
    return {"ok": True, "message": "Request removed." + warn}


@router.post("/{rid}/retry")
async def retry(rid: int, user: CurrentUser = Depends(require_user)) -> dict:
    row = _get_visible(rid, user)
    if row["status"] == "available":
        raise ApiError(409, "already_available", "That album is already available.")
    try:
        message = await orchestrate.retry(row)
    except httpx.HTTPError as e:
        raise UpstreamError("lidarr", "Couldn't reach Lidarr to retry — try again shortly.") from e
    return {"ok": True, "message": message}


@router.post("/{rid}/search-now")
async def search_now(rid: int, user: CurrentUser = Depends(require_user)) -> dict:
    row = _get_visible(rid, user)
    if row["status"] == "available":
        raise ApiError(409, "already_available", "That album is already available.")
    if row["status"] == "error":
        raise ApiError(409, "use_retry", "Use retry for a request that couldn't be added.")
    try:
        message = await orchestrate.search_now(row)
    except httpx.HTTPError as e:
        raise UpstreamError("lidarr") from e
    return {"ok": True, "message": message}


@router.post("/{rid}/drop-download")
async def drop_download(rid: int, user: CurrentUser = Depends(require_user)) -> dict:
    """Remove the current Lidarr queue item (from the client, blocklisted). The album stays
    wanted; Soularr, which skips albums already in the queue, can then try Soulseek."""
    row = _get_visible(rid, user)
    if not row.get("lidarr_album_id"):
        raise ApiError(409, "nothing_to_drop", "This request has no download yet.")
    q = (await lidarr.queue_index()).get(row["lidarr_album_id"])
    if not q or q.get("id") is None:
        raise ApiError(409, "nothing_to_drop", "Nothing is downloading for this request.")
    try:
        await lidarr.remove_queue_item(q["id"])
    except httpx.HTTPError as e:
        raise UpstreamError("lidarr") from e
    req_db.add_event(rid, "download_dropped", {"queueId": q["id"], "title": q.get("title"),
                                               "by": user.username})
    row = req_db.update(rid, status="searching")
    orchestrate.broadcast(row)
    from ..upstream import soularr
    await soularr.try_trigger_run(f"drop-download {rid}")
    return {"ok": True, "message": "Dropped that download — still searching (Soulseek next)."}
