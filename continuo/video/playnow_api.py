"""Play now for films and episodes, over HTTP: start it, follow it, and the bytes as they arrive."""
import asyncio
import json
import os
import re
import time

import httpx
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import config
from ..auth.deps import CurrentUser, require_user
from ..errors import ApiError
from . import db, playnow

router = APIRouter(prefix="/v1/video/play-now", tags=["video"])
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


_tasks: set[asyncio.Task] = set()


class PlayNowIn(BaseModel):
    tmdbId: int
    kind: str = "movie"
    # An episode: which one (a series is requested by season; Sonarr fetches the season).
    season: int | None = None
    episode: int | None = None


def _ep(kind: str, season: int | None, episode: int | None) -> playnow.Ep:
    if kind == "movie":
        return None
    if kind != "series" or season is None or episode is None or season < 1 or episode < 1:
        raise ApiError(400, "bad_episode", "Play now for a series needs a season and an episode.")
    return (season, episode)


@router.post("")
async def start(body: PlayNowIn, user: CurrentUser = Depends(require_user)) -> dict:
    """A film or an episode not in the library, wanted now: requested (a series: its season), and
    played as it downloads once enough has come. One in the library just plays."""
    ep = _ep(body.kind, body.season, body.episode)
    jellyfin_id = await playnow.in_library({"tmdb_id": body.tmdbId}, ep)
    if jellyfin_id:
        return {"state": "available", "jellyfinId": jellyfin_id, "requestId": None}
    from .api import RequestIn, request_title
    out = await request_title(RequestIn(kind=body.kind, tmdbId=body.tmdbId, seasons=[ep[0]] if ep else None), user,
                              play_now=ep is None)
    playnow.mark_latest(out["request"]["id"], ep)
    if ep and not out["created"]:
        task = asyncio.get_running_loop().create_task(_ensure_season(out["request"]["id"], ep[0]))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"state": "searching", "requestId": out["request"]["id"], "created": out["created"]}


async def _ensure_season(request_id: int, season: int) -> None:
    """A series already on its way, but not this season: add the season to the request and Sonarr."""
    from .upstream import sonarr, tmdb
    req = db.get(request_id)
    asked = json.loads(req["seasons"]) if req and req.get("seasons") else None
    if req is None or asked is None or season in asked:
        return
    db.update(request_id, seasons=json.dumps(sorted({*asked, season})))
    try:
        show = await tmdb.series(req["tmdb_id"])
        tvdb = (show.get("external_ids") or {}).get("tvdb_id")
        if tvdb:
            await sonarr.request(int(tvdb), [season], anime=tmdb.is_anime(show))
    except (httpx.HTTPError, tmdb.Unavailable):
        pass


def _get(request_id: int, ep: playnow.Ep) -> dict:
    req = db.get(request_id)
    if not req or req["kind"] != ("movie" if ep is None else "series"):
        raise ApiError(404, "not_found", "No such request.")
    return req


@router.get("/{request_id}")
async def follow(request_id: int, season: int | None = Query(None, ge=1), episode: int | None = Query(None, ge=1),
                 user: CurrentUser = Depends(require_user)) -> dict:
    """Where it stands; once ready, a link the player streams from (signed for this account)."""
    ep = (season, episode) if season and episode else None
    req = _get(request_id, ep)
    if req["status"] in db.ACTIVE:
        playnow.mark_latest(request_id, ep)            # being looked at: it's the latest
    out = await playnow.status(req, ep)
    if out["state"] == "ready":
        expires = int(time.time()) + playnow.LINK_SECONDS
        base = config.settings.public_base_url
        which = f"&s={ep[0]}&e={ep[1]}" if ep else ""
        out["streamUrl"] = (f"{base}/v1/video/play-now/{request_id}/stream?u={user.id}&exp={expires}{which}"
                            f"&sig={playnow.signature(request_id, user.id, expires, ep)}")
        out["expiresAt"] = expires
    return {"requestId": request_id, "title": req.get("title"), **out}


@router.get("/{request_id}/stream")
async def stream(request: Request, request_id: int = Path(...), u: int = Query(...), exp: int = Query(...),
                 sig: str = Query(..., pattern="^[0-9a-f]{64}$"), s: int | None = Query(None, ge=1),
                 e: int | None = Query(None, ge=1)) -> StreamingResponse:
    """The file, as far as it has arrived: a range starting past that waits a little for it (the
    torrent comes in order), then answers 503 so the player tries again."""
    ep = (s, e) if s and e else None
    if not playnow.verify(request_id, u, exp, sig, ep):
        raise ApiError(403, "bad_link", "This link has expired or isn't valid.")
    req = _get(request_id, ep)
    m = _RANGE.fullmatch((request.headers.get("range") or "bytes=0-").strip())
    if not m:
        raise ApiError(416, "bad_range", "Only one byte range, please.")
    start = int(m.group(1) or 0)
    try:
        layout, available = await playnow.available(req, start, ep)
    except LookupError as e:
        raise ApiError(409, "not_ready", str(e), True) from e
    except TimeoutError as e:
        raise ApiError(503, "still_downloading", str(e), True, retryAfter=3) from e
    if start >= layout.size:
        raise ApiError(416, "bad_range", "That's past the end of the film.")
    want_end = int(m.group(2)) + 1 if m.group(2) else layout.size
    end = min(want_end, available, start + playnow.CHUNK_BYTES, layout.size)
    path = layout.path

    async def body():
        with open(path, "rb") as f:
            await run_in_threadpool(f.seek, start)
            left = end - start
            while left > 0:
                chunk = await run_in_threadpool(f.read, min(1024 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers = {"Content-Range": f"bytes {start}-{end - 1}/{layout.size}", "Accept-Ranges": "bytes",
               "Content-Length": str(end - start), "Cache-Control": "no-store",
               "Content-Disposition": f'inline; filename="{os.path.basename(layout.name)}"'}
    return StreamingResponse(body(), status_code=206, media_type=layout.mime, headers=headers)


@router.post("/{request_id}/faster")
async def faster(request_id: int, _user: CurrentUser = Depends(require_user)) -> dict:
    """The film is coming too slowly to watch: swap its download for the best release to stream
    (the slow one blocklisted)."""
    req = _get(request_id, None)
    try:
        chosen = await playnow.find_faster(req)
    except httpx.HTTPError as e:
        raise ApiError(502, "radarr_unavailable", "Radarr isn't answering.", True) from e
    if not chosen:
        raise ApiError(404, "nothing_faster", "There's no better-seeded copy to stream right now.")
    return {"title": chosen.get("title"), "seeders": chosen.get("seeders"), "sizeBytes": chosen.get("size")}


@router.post("/{request_id}/stop")
async def stop(request_id: int, season: int | None = Query(None, ge=1), episode: int | None = Query(None, ge=1),
               _user: CurrentUser = Depends(require_user)) -> dict:
    """Not wanted after all: stop the download and delete what it had (and, for a film Radarr has no
    file for, take it out of Radarr; for an episode, stop Sonarr wanting its season)."""
    ep = (season, episode) if season and episode else None
    req = db.get(request_id)
    if not req:
        raise ApiError(404, "not_found", "No such request.")
    try:
        await playnow.stop(req, ep)
    except httpx.HTTPError as e:
        raise ApiError(502, "arr_unavailable", "Radarr or Sonarr didn't answer: try again.", True) from e
    await playnow.rebalance()
    return {"stopped": True}
