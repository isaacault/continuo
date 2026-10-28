"""Scrobbling an account's Play now songs (library songs are Navidrome's to scrobble).

GET /v1/scrobbling says what's linked; ListenBrainz links with the account's user token, Last.fm
through its web approval (connect -> the user approves at the URL -> finish)."""
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth.deps import CurrentUser, require_user
from ..db import scrobblers as db
from ..errors import ApiError
from ..upstream import scrobble

router = APIRouter(prefix="/v1/scrobbling", tags=["scrobbling"])


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


def _status(user_id: int) -> dict:
    linked = db.all_for(user_id)
    return {
        "listenbrainz": {"available": True, "connected": "listenbrainz" in linked,
                         "account": (linked.get("listenbrainz") or {}).get("account")},
        "lastfm": {"available": scrobble.lastfm_available(), "connected": "lastfm" in linked,
                   "account": (linked.get("lastfm") or {}).get("account")},
    }


@router.get("")
async def status(user: CurrentUser = Depends(require_user)) -> dict:
    return _status(user.id)


@router.put("/listenbrainz")
async def link_listenbrainz(body: TokenIn, user: CurrentUser = Depends(require_user)) -> dict:
    """Link with the account's ListenBrainz user token (listenbrainz.org, Settings)."""
    try:
        name = await scrobble.listenbrainz_user(body.token.strip())
    except scrobble.Refused as e:
        raise ApiError(400, "token_refused", str(e)) from e
    except httpx.HTTPError as e:
        raise ApiError(502, "listenbrainz_unavailable", "ListenBrainz can't be reached right now.", True) from e
    db.put(user.id, "listenbrainz", body.token.strip(), name)
    return _status(user.id)


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


@router.post("/lastfm/finish")
async def lastfm_finish(body: TokenIn, user: CurrentUser = Depends(require_user)) -> dict:
    try:
        key, name = await scrobble.lastfm_finish(body.token.strip())
    except scrobble.Refused as e:
        raise ApiError(409, "not_approved", "Last.fm hasn't been approved yet: approve it in the browser, then try again.") from e
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise ApiError(502, "lastfm_unavailable", "Last.fm can't be reached right now.", True) from e
    db.put(user.id, "lastfm", key, name)
    return _status(user.id)


@router.delete("/{service}")
async def unlink(service: str, user: CurrentUser = Depends(require_user)) -> dict:
    if service not in ("listenbrainz", "lastfm"):
        raise ApiError(404, "not_found", "No such service.")
    db.delete(user.id, service)
    return _status(user.id)
