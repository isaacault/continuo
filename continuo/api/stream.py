"""Adaptive streaming (polyphony#185): any library song as HLS, Opus at several bitrates.

    GET  /v1/stream/{song}/master.m3u8[?max=&min=]   the renditions (kbit/s within the limits)
    GET  /v1/stream/{song}/{kbps}/index.m3u8         a complete VOD playlist of 6 s segments
    GET  /v1/stream/{song}/{kbps}/init.mp4 | N.m4s   the init segment (gapless) and the segments
    POST /v1/stream/upcoming                          the songs next in the player's queue

The player (Media3's HLS) picks a rendition from the throughput it measures, segment by segment,
so quality follows the connection inside a song, on Wi-Fi as on mobile data. When the connection
comes and goes, the phone drops to a low level and posts what's coming up; continuo encodes those
songs ahead so they're on disk the moment the signal returns. Only the current request is kept:
no history of connections. Songs are Navidrome ids; the bearer token's user must be able to read
them in Navidrome (getSong as that user).
"""
import asyncio
import logging
import re

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, require_user
from ..errors import ApiError
from ..stream import encoder, playlists
from ..upstream import navidrome

log = logging.getLogger("continuo")
router = APIRouter(prefix="/v1/stream", tags=["stream"])

_FILE = re.compile(r"^(init\.mp4|\d{1,5}\.m4s)$")
# How many of the songs coming up are encoded ahead, and at most how many are accepted.
AHEAD = 4
_upcoming: dict[str, set[tuple[str, int]]] = {}


class UpcomingIn(BaseModel):
    songs: list[str] = Field(default_factory=list, max_length=50)
    # The level the player is on now; songs ahead are encoded at it.
    kbps: int = Field(64, ge=1, le=10_000)
    # The connection is coming and going: also encode the next song one level lower.
    lowerToo: bool = False


def _song(song: str) -> str:
    if not encoder.valid_id(song):
        raise ApiError(404, "not_found", "No such song.")
    return song


def _level(kbps: int) -> int:
    if kbps not in playlists.LADDER:
        raise ApiError(404, "not_found", "No such rendition.")
    return kbps


@router.get("/{song}/master.m3u8")
async def master(song: str, max: int | None = Query(None, ge=1),  # noqa: A002
                 min: int | None = Query(None, ge=1),  # noqa: A002
                 user: CurrentUser = Depends(require_user)) -> Response:
    _song(song)
    try:
        body = await navidrome.subsonic("getSong", {"id": song}, as_user=user.navidrome_username)
    except navidrome.NavidromeError as e:
        raise ApiError(404, "not_found", "No such song.") from e
    s = body.get("song") or {}
    encoder.check_source(song, f"{s.get('size')}:{s.get('suffix')}:{s.get('duration')}")
    encoder.touch(song)
    return Response(playlists.master(playlists.levels(max, min)), media_type=playlists.MPEGURL,
                    headers={"Cache-Control": "no-store"})


async def _ready(song: str, kbps: int, user: CurrentUser) -> encoder.Job:
    job = encoder.ensure(_song(song), _level(kbps), user.navidrome_username)
    try:
        return await encoder.wait_ready(job)
    except asyncio.TimeoutError as e:
        raise ApiError(504, "encoding", "Still encoding; try again.") from e
    except encoder.EncodeError as e:
        raise ApiError(502, "encode_failed", "This song couldn't be encoded.") from e


@router.get("/{song}/{kbps}/index.m3u8")
async def media(song: str, kbps: int, user: CurrentUser = Depends(require_user)) -> Response:
    job = await _ready(song, kbps, user)
    return Response(playlists.media(job.segments), media_type=playlists.MPEGURL,
                    headers={"Cache-Control": "no-store"})


@router.get("/{song}/{kbps}/{name}")
async def segment(song: str, kbps: int, name: str,
                  user: CurrentUser = Depends(require_user)) -> FileResponse:
    if not _FILE.match(name):
        raise ApiError(404, "not_found", "No such file.")
    job = await _ready(song, kbps, user)
    try:
        path = await encoder.wait_file(job, name)
    except asyncio.TimeoutError as e:
        raise ApiError(504, "encoding", "Still encoding; try again.") from e
    except encoder.EncodeError as e:
        raise ApiError(502, "encode_failed", "This song couldn't be encoded.") from e
    if path is None:
        raise ApiError(404, "not_found", "No such segment.")
    return FileResponse(path, media_type="audio/mp4",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.post("/upcoming")
async def upcoming(body: UpcomingIn, user: CurrentUser = Depends(require_user)) -> dict:
    """Encode the next songs in the player's queue ahead, at the level it is on now (the nearest
    rung at or below), and forget earlier songs-ahead of this device that haven't started."""
    kbps = max([k for k in playlists.LADDER if k <= body.kbps] or [playlists.LADDER[0]])
    lower = [k for k in playlists.LADDER if k < kbps]
    wanted: list[tuple[str, int]] = []
    for i, song in enumerate(s for s in body.songs if encoder.valid_id(s)):
        if i >= AHEAD:
            break
        wanted.append((song, kbps))
        if i == 0 and body.lowerToo and lower:
            wanted.append((song, lower[-1]))
    for key in _upcoming.get(user.device_id, set()) - set(wanted):
        encoder.cancel_waiting(*key)
    _upcoming[user.device_id] = set(wanted)
    for song, k in wanted:
        encoder.touch(song)
        encoder.ensure(song, k, user.navidrome_username, prefetch=True)
    return {"encoding": [{"song": s, "kbps": k} for s, k in wanted]}
