"""Play now (#12): start a stream, follow it, play its audio while it downloads, keep it."""
import asyncio
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, require_user
from ..db import streams as streams_db
from ..db import users as users_db
from ..errors import ApiError
from ..requests import playnow, view

router = APIRouter(prefix="/v1/play-now", tags=["play-now"])

_TYPES = {"flac": "audio/flac", "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg",
          "opus": "audio/ogg", "wav": "audio/wav"}
# While the file is still downloading, how long a read waits for more bytes before giving up.
_STALL_S = 30.0
_CHUNK = 64 * 1024


class PlayNowIn(BaseModel):
    musicBrainzRecordingId: str = Field(..., min_length=8, max_length=64)
    musicBrainzReleaseGroupId: Optional[str] = Field(None, min_length=8, max_length=64)
    title: str = Field(..., min_length=1, max_length=300)
    artist: str = Field(..., min_length=1, max_length=300)
    durationMs: Optional[int] = None


def _mine(stream_id: str, user: CurrentUser) -> dict:
    row = streams_db.get(stream_id)
    if row is None or (row["user_id"] not in (None, user.id) and not user.is_admin):
        raise ApiError(404, "not_found", "No such stream.")
    return row


@router.post("", status_code=201)
async def start(body: PlayNowIn, user: CurrentUser = Depends(require_user)) -> dict:
    """Start fetching a song to play now; poll GET /{id} until `playable`, then play /{id}/audio."""
    from ..upstream import slskd
    if not slskd.enabled():
        raise ApiError(409, "unavailable", "This server can't fetch songs to play right now.")
    row = playnow.start(user.id, body.musicBrainzRecordingId.lower(),
                        body.musicBrainzReleaseGroupId.lower() if body.musicBrainzReleaseGroupId else None,
                        body.title, body.artist, body.durationMs)
    return playnow.view(row)


@router.get("/{stream_id}")
async def status(stream_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    return playnow.view(_mine(stream_id, user))


@router.post("/{stream_id}/keep")
async def keep(stream_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    """Into your library: a single-song request that files this copy (nothing downloads again)."""
    _mine(stream_id, user)
    try:
        req = playnow.keep(users_db.get_by_id(user.id), stream_id)
    except LookupError as e:
        raise ApiError(409, "not_ready", str(e)) from e
    from ..requests import tracks
    return view.to_out(req, tracks.pipeline(req), user.username)


@router.get("/{stream_id}/audio")
async def audio(stream_id: str, request: Request, user: CurrentUser = Depends(require_user)):
    """The song's audio, served while it downloads: the full length is announced (the copy's size
    on Soulseek), and a read past what's on disk waits for the download (up to 30 s without a new
    byte). Range requests work, so the player can seek."""
    row = _mine(stream_id, user)
    if row["status"] not in ("streaming", "ready"):
        raise ApiError(409, "not_ready", "Not playable yet.")
    size = row.get("size") or 0
    path = playnow.audio_path(row)
    if not path:
        raise ApiError(410, "gone", "That song is no longer cached.")
    if row["status"] == "ready" or not size:
        size = os.path.getsize(path)
    start, end = 0, size - 1
    m = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("range", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:                                   # the last N bytes
            start = max(0, size - int(m.group(2)))
        if start >= size:
            raise ApiError(416, "range", "Out of range.")
    media = _TYPES.get((row.get("extension") or "").lower(), "application/octet-stream")
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if m:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    async def body():
        pos, idle = start, 0.0
        current = path
        f = open(current, "rb")
        try:
            while pos <= end:
                f.seek(pos)
                chunk = f.read(min(_CHUNK, end - pos + 1))
                if chunk:
                    pos += len(chunk)
                    idle = 0.0
                    yield chunk
                    continue
                # At the end of what's on disk: wait for the download (or its move to the cache).
                if idle >= _STALL_S:
                    return
                await asyncio.sleep(0.25)
                idle += 0.25
                latest = streams_db.get(stream_id)
                moved = playnow.audio_path(latest) if latest else None
                if moved and moved != current and os.path.exists(moved):
                    f.close()
                    current, f = moved, open(moved, "rb")
        finally:
            f.close()

    return StreamingResponse(body(), status_code=206 if m else 200, media_type=media, headers=headers)
