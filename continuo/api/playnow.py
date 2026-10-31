"""Play now (#12): start a stream, follow it, play its audio while it downloads, keep it."""
import asyncio
import hashlib
import hmac
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from .. import config
from ..auth import deps
from ..auth.deps import CurrentUser, require_user
from ..db import devices as devices_db
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
    # True: queued behind this account's other queued songs (polyphony#164); else played now.
    queued: bool = False


class PriorityIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=500)


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
                        body.title, body.artist, body.durationMs, queued=body.queued)
    return playnow.view(row)


@router.post("/priority")
async def priority(body: PriorityIn, user: CurrentUser = Depends(require_user)) -> dict:
    """The order the app will play its queued Play now songs in (soonest first): fetched in it."""
    return {"updated": playnow.prioritise(user.id, body.ids)}


@router.get("/{stream_id}")
async def status(stream_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    playnow.kick(user.id)
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


class ScrobbleIn(BaseModel):
    # Epoch seconds the song started: a finished listen. Absent: it's playing now.
    listenedAt: Optional[int] = Field(None, ge=0)


@router.post("/{stream_id}/scrobble")
async def scrobble(stream_id: str, body: ScrobbleIn, user: CurrentUser = Depends(require_user)) -> dict:
    """Scrobble a Play now song (Navidrome can't: it isn't a library song) to the account's linked
    ListenBrainz and Last.fm. Best-effort: {sent: [services]}; a failure is logged, not an error."""
    row = _mine(stream_id, user)
    return {"sent": await playnow.scrobble(user.id, row, body.listenedAt)}


# A receiver (DLNA, Cast) fetches the audio itself and can't send a bearer token: it's given a link
# signed for this stream and this device, good for RECEIVER_LINK_SECONDS, dead once the device is
# signed out (GET /{id}/receiver).
RECEIVER_LINK_SECONDS = 12 * 3600


def _signature(stream_id: str, device_id: str, expires: int) -> str:
    msg = f"play-now-audio|{stream_id}|{device_id}|{expires}".encode()
    return hmac.new(config.settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()


def _signed_user(stream_id: str, device_id: str | None, expires: int | None, sig: str | None) -> CurrentUser | None:
    if not (device_id and expires and sig) or expires < time.time():
        return None
    if not hmac.compare_digest(sig, _signature(stream_id, device_id, expires)):
        return None
    device = devices_db.get(device_id)
    if not device or device["revoked_at"]:
        return None
    user = users_db.get_by_id(device["user_id"])
    if not user:
        return None
    return CurrentUser(user["id"], user["username"], user["role"],
                       user.get("navidrome_username") or user["username"], device_id)


async def _audio_user(stream_id: str, request: Request,
                      creds: HTTPAuthorizationCredentials | None = Depends(deps._bearer),
                      d: str | None = Query(None), exp: int | None = Query(None),
                      sig: str | None = Query(None)) -> CurrentUser:
    """The bearer token's user, or a receiver link's (its device's user)."""
    if creds is not None:
        return deps.require_user(request, creds)
    user = _signed_user(stream_id, d, exp, sig)
    if user is None:
        raise ApiError(401, "missing_token", "Missing or expired token.")
    return user


@router.get("/{stream_id}/receiver")
async def receiver_link(stream_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    """A link a speaker can play without a token: on RECEIVER_BASE_URL (the plain-HTTP LAN door)
    when set, else continuo's public address; signed for this device, good for 12 hours."""
    _mine(stream_id, user)
    expires = int(time.time()) + RECEIVER_LINK_SECONDS
    base = config.settings.receiver_base_url or config.settings.public_base_url
    query = f"d={user.device_id}&exp={expires}&sig={_signature(stream_id, user.device_id, expires)}"
    return {"url": f"{base}/v1/play-now/{stream_id}/audio?{query}", "expiresAt": expires}


@router.get("/{stream_id}/audio")
async def audio(stream_id: str, request: Request, user: CurrentUser = Depends(_audio_user)):
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
