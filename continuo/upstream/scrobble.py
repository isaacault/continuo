"""Scrobbling Play now songs to ListenBrainz and Last.fm, for the account that played them.

ListenBrainz takes the account's user token. Last.fm needs continuo's API key and secret
(LASTFM_API_KEY, LASTFM_SECRET: the same key Navidrome uses) and a session key per account, got
through Last.fm's web authorisation: auth.getToken, the user approves at last.fm/api/auth, then
auth.getSession. Calls are best-effort: a failed scrobble is logged, never an error for the app."""
import hashlib
import logging
from typing import Optional

import httpx

from .. import config
from . import http

log = logging.getLogger("continuo")

_LB = "https://api.listenbrainz.org/1"
_LASTFM = "https://ws.audioscrobbler.com/2.0/"
_CLIENT = "Polyphony (Play now via continuo)"


def _lb() -> httpx.AsyncClient:
    return http.client("listenbrainz-scrobble", base_url=_LB, timeout=10.0,
                       headers={"User-Agent": config.settings.musicbrainz_user_agent})


def _fm() -> httpx.AsyncClient:
    return http.client("lastfm", timeout=10.0, headers={"User-Agent": config.settings.musicbrainz_user_agent})


def lastfm_available() -> bool:
    return bool(config.settings.lastfm_api_key and config.settings.lastfm_secret)


class Refused(Exception):
    """The service refused the credentials (a wrong token, an unapproved Last.fm token)."""


# ---- ListenBrainz ---------------------------------------------------------------------------

async def listenbrainz_user(token: str) -> str:
    """The ListenBrainz user name for [token]; Refused when it isn't valid."""
    r = await _lb().get("/validate-token", headers={"Authorization": f"Token {token}"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not body.get("valid"):
        raise Refused("That ListenBrainz token isn't valid.")
    return body.get("user_name") or ""


async def listenbrainz(token: str, song: dict, listened_at: Optional[int]) -> None:
    """A listen ([listened_at], epoch seconds) or, without it, what's playing now."""
    meta = {"artist_name": song["artist"], "track_name": song["title"],
            "additional_info": {"submission_client": _CLIENT, "recording_mbid": song.get("recordingMbid"),
                                "duration_ms": song.get("durationMs")}}
    meta["additional_info"] = {k: v for k, v in meta["additional_info"].items() if v}
    listen = {"track_metadata": meta}
    if listened_at:
        listen["listened_at"] = listened_at
    body = {"listen_type": "single" if listened_at else "playing_now", "payload": [listen]}
    r = await _lb().post("/submit-listens", json=body, headers={"Authorization": f"Token {token}"})
    r.raise_for_status()


# ---- Last.fm --------------------------------------------------------------------------------

def _signed(params: dict) -> dict:
    """[params] with api_key, api_sig (md5 of the sorted name+value pairs and the secret) and format."""
    p = {**params, "api_key": config.settings.lastfm_api_key}
    sig = "".join(f"{k}{p[k]}" for k in sorted(p)) + config.settings.lastfm_secret
    return {**p, "api_sig": hashlib.md5(sig.encode("utf-8")).hexdigest(), "format": "json"}


async def _lastfm(method: str, params: dict, post: bool = False) -> dict:
    q = _signed({"method": method, **params})
    r = await (_fm().post(_LASTFM, data=q) if post else _fm().get(_LASTFM, params=q))
    body = r.json()
    if "error" in body:
        if body.get("error") in (4, 9, 14, 15):        # authentication / token problems
            raise Refused(body.get("message") or "Last.fm refused.")
        raise httpx.HTTPStatusError(body.get("message") or "Last.fm error", request=r.request, response=r)
    return body


async def lastfm_start() -> tuple[str, str]:
    """(token, the URL where the user approves continuo on Last.fm)."""
    token = (await _lastfm("auth.getToken", {}))["token"]
    return token, f"https://www.last.fm/api/auth/?api_key={config.settings.lastfm_api_key}&token={token}"


async def lastfm_finish(token: str) -> tuple[str, str]:
    """(session key, user name) once the user approved [token]; Refused before that."""
    s = (await _lastfm("auth.getSession", {"token": token}))["session"]
    return s["key"], s.get("name") or ""


async def lastfm(session_key: str, song: dict, listened_at: Optional[int]) -> None:
    params = {"artist": song["artist"], "track": song["title"], "sk": session_key}
    if song.get("durationMs"):
        params["duration"] = str(round(song["durationMs"] / 1000))
    if song.get("recordingMbid"):
        params["mbid"] = song["recordingMbid"]
    if listened_at:
        await _lastfm("track.scrobble", {**params, "timestamp": str(listened_at)}, post=True)
    else:
        await _lastfm("track.updateNowPlaying", params, post=True)
