"""Scrobbling an account's Play now songs (library songs are Navidrome's to scrobble).

GET /v1/scrobbling says what's linked; ListenBrainz links with the account's user token, Last.fm
through its web approval (connect -> the user approves at the URL -> finish). It also says where
Navidrome scrobbles the account's library songs ("library"), so the phone only scrobbles a song
itself to services nobody else covers (#122)."""
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, require_user
from ..db import scrobblers as db
from ..errors import ApiError
from ..search.cache import TTLCache
from ..upstream import navidrome, scrobble

log = logging.getLogger("continuo")

router = APIRouter(prefix="/v1/scrobbling", tags=["scrobbling"])


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class ServiceOut(BaseModel):
    available: bool
    connected: bool
    account: Optional[str]


class LibraryOut(BaseModel):
    """Whether Navidrome scrobbles this account's library songs to each service."""
    listenbrainz: bool
    lastfm: bool


class StatusOut(BaseModel):
    listenbrainz: ServiceOut
    lastfm: ServiceOut
    # Null when continuo can't tell (Navidrome unreachable, or password auth).
    library: Optional[LibraryOut] = None


_STATUS = {200: {"model": StatusOut}}

# Navidrome's links, per (account, Navidrome name): they change when someone links a service in
# Navidrome's UI, rarely. An answer is kept a few minutes, a failure briefly (no retry storm).
LIBRARY_TTL_S = 300
LIBRARY_FAILED_TTL_S = 30
_library_cache = TTLCache(LIBRARY_TTL_S, max_items=256)
_library_failed = TTLCache(LIBRARY_FAILED_TTL_S, max_items=256)


async def _navidrome_link(service: str, nd_user: str) -> bool:
    """GET /api/<service>/link as the account: {"status": bool, ...}, true when linked.
    Navidrome mounts the route only when the service is enabled server-wide, so its 404 means
    Navidrome doesn't scrobble there for anyone."""
    try:
        data, _ = await navidrome.native_get(f"{service}/link", as_user=nd_user)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return False
        raise
    if not isinstance(data, dict) or not isinstance(data.get("status"), bool):
        raise ValueError(f"unexpected {service}/link answer")
    return data["status"]


async def library_links(user: CurrentUser) -> Optional[dict]:
    """{listenbrainz, lastfm}: where Navidrome scrobbles the account's library songs, or None
    when Navidrome can't say. Never raises."""
    key = (user.id, user.navidrome_username)
    hit = _library_cache.get(key)
    if hit is not None:
        return dict(hit)
    if _library_failed.get(key):
        return None
    lb, fm = await asyncio.gather(_navidrome_link("listenbrainz", user.navidrome_username),
                                  _navidrome_link("lastfm", user.navidrome_username),
                                  return_exceptions=True)
    for answer in (lb, fm):
        if isinstance(answer, (navidrome.NavidromeError, httpx.HTTPError, ValueError)):
            log.warning("scrobbling: Navidrome's links for %s unknown: %s", user.username,
                        answer.__class__.__name__)
            _library_failed.put(key, True)
            return None
        if isinstance(answer, BaseException):
            raise answer
    out = {"listenbrainz": lb, "lastfm": fm}
    _library_cache.put(key, out)
    return dict(out)


async def _status(user: CurrentUser) -> dict:
    linked = db.all_for(user.id)
    return {
        "listenbrainz": {"available": True, "connected": "listenbrainz" in linked,
                         "account": (linked.get("listenbrainz") or {}).get("account")},
        "lastfm": {"available": scrobble.lastfm_available(), "connected": "lastfm" in linked,
                   "account": (linked.get("lastfm") or {}).get("account")},
        "library": await library_links(user),
    }


@router.get("", responses=_STATUS)
async def status(user: CurrentUser = Depends(require_user)) -> dict:
    """What's linked for Play now songs, and in "library" where Navidrome scrobbles the account's
    library songs (null when it can't be told): the phone scrobbles a song itself only to
    services neither covers."""
    return await _status(user)


@router.put("/listenbrainz", responses=_STATUS)
async def link_listenbrainz(body: TokenIn, user: CurrentUser = Depends(require_user)) -> dict:
    """Link with the account's ListenBrainz user token (listenbrainz.org, Settings)."""
    try:
        name = await scrobble.listenbrainz_user(body.token.strip())
    except scrobble.Refused as e:
        raise ApiError(400, "token_refused", str(e)) from e
    except httpx.HTTPError as e:
        raise ApiError(502, "listenbrainz_unavailable", "ListenBrainz can't be reached right now.", True) from e
    db.put(user.id, "listenbrainz", body.token.strip(), name)
    return await _status(user)


@router.post("/lastfm/connect")
async def lastfm_connect(user: CurrentUser = Depends(require_user)) -> dict:
    """{token, url}: open url, approve continuo on Last.fm, then POST /lastfm/finish with token."""
    if not scrobble.lastfm_available():
        raise ApiError(409, "lastfm_unavailable", "This server has no Last.fm API key.")
    try:
        token, url = await scrobble.lastfm_start()
    except (httpx.HTTPError, scrobble.Refused, KeyError, ValueError) as e:
        raise ApiError(502, "lastfm_unavailable", "Last.fm can't be reached right now.", True) from e
    return {"token": token, "url": url}


@router.post("/lastfm/finish", responses=_STATUS)
async def lastfm_finish(body: TokenIn, user: CurrentUser = Depends(require_user)) -> dict:
    try:
        key, name = await scrobble.lastfm_finish(body.token.strip())
    except scrobble.Refused as e:
        raise ApiError(409, "not_approved", "Last.fm hasn't been approved yet: approve it in the browser, then try again.") from e
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise ApiError(502, "lastfm_unavailable", "Last.fm can't be reached right now.", True) from e
    db.put(user.id, "lastfm", key, name)
    return await _status(user)


@router.delete("/{service}", responses=_STATUS)
async def unlink(service: str, user: CurrentUser = Depends(require_user)) -> dict:
    if service not in ("listenbrainz", "lastfm"):
        raise ApiError(404, "not_found", "No such service.")
    db.delete(user.id, service)
    return await _status(user)
