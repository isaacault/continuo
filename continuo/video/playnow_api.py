"""Play now for films, over HTTP: start it, follow it, and the film's bytes as they arrive."""
import os
import re
import time

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import config
from ..auth.deps import CurrentUser, require_user
from ..errors import ApiError
from . import db, index, playnow

router = APIRouter(prefix="/v1/video/play-now", tags=["video"])
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class PlayNowIn(BaseModel):
    tmdbId: int


@router.post("")
async def start(body: PlayNowIn, user: CurrentUser = Depends(require_user)) -> dict:
    """A film not in the library, wanted now: requested (or the request already on its way), and
    played as it downloads once enough has come. One in the library just plays."""
    item = index.by_tmdb("movie", body.tmdbId)
    if item:
        return {"state": "available", "jellyfinId": item["jellyfin_id"], "requestId": None}
    from .api import RequestIn, create_request
    out = await create_request(RequestIn(kind="movie", tmdbId=body.tmdbId), user)
    return {"state": "searching", "requestId": out["request"]["id"], "created": out["created"]}


def _get(request_id: int) -> dict:
    req = db.get(request_id)
    if not req or req["kind"] != "movie":
        raise ApiError(404, "not_found", "No such film request.")
    return req


@router.get("/{request_id}")
async def follow(request_id: int, user: CurrentUser = Depends(require_user)) -> dict:
    """Where it stands; once ready, a link the player streams from (signed for this account)."""
    req = _get(request_id)
    out = await playnow.status(req)
    if out["state"] == "ready":
        expires = int(time.time()) + playnow.LINK_SECONDS
        base = config.settings.public_base_url
        out["streamUrl"] = (f"{base}/v1/video/play-now/{request_id}/stream?u={user.id}&exp={expires}"
                            f"&sig={playnow.signature(request_id, user.id, expires)}")
        out["expiresAt"] = expires
    return {"requestId": request_id, "title": req.get("title"), **out}


@router.get("/{request_id}/stream")
async def stream(request: Request, request_id: int = Path(...), u: int = Query(...), exp: int = Query(...),
                 sig: str = Query(..., pattern="^[0-9a-f]{64}$")) -> StreamingResponse:
    """The film's file, as far as it has arrived: a range starting past that waits a little for it
    (the torrent comes in order), then answers 503 so the player tries again."""
    if not playnow.verify(request_id, u, exp, sig):
        raise ApiError(403, "bad_link", "This link has expired or isn't valid.")
    req = _get(request_id)
    m = _RANGE.fullmatch((request.headers.get("range") or "bytes=0-").strip())
    if not m:
        raise ApiError(416, "bad_range", "Only one byte range, please.")
    start = int(m.group(1) or 0)
    try:
        layout, available = await playnow.available(req, start)
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
