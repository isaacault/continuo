"""Deezer's public search (no key, no account): what ranks search. It knows what's popular
("sabrina" is Sabrina Carpenter), finds songs and albums by title ("espresso", "ok computer"),
forgives typos ("beyonse") and answers in about 0.15 s, where MusicBrainz's search has no sense
of popularity and is paced to a call a second. Deezer's hits carry no MusicBrainz IDs; the
search maps them (search.ranked). Only continuo calls it: phones talk to continuo alone.

Deezer allows 50 requests in 5 s per address; continuo keeps under it with its own budget and
raises Exhausted past it, so a search falls back to MusicBrainz rather than being refused."""
import time
from collections import deque
from typing import Optional

import httpx

from .. import config
from . import http

_URL = "https://api.deezer.com"
_BUDGET, _WINDOW_S = 45, 5.0
_sent: deque[float] = deque()


class Exhausted(Exception):
    """Over continuo's share of Deezer's rate limit for now."""


class DeezerError(ValueError):
    """Deezer answered with an error (its quota, a bad query)."""


def _client() -> httpx.AsyncClient:
    return http.client("deezer", base_url=_URL, timeout=4.0,
                       headers={"User-Agent": config.settings.musicbrainz_user_agent, "Accept": "application/json"})


def _spend() -> None:
    now = time.monotonic()
    while _sent and now - _sent[0] > _WINDOW_S:
        _sent.popleft()
    if len(_sent) >= _BUDGET:
        raise Exhausted()
    _sent.append(now)


def reset() -> None:
    _sent.clear()


async def _search(path: str, q: str, limit: int) -> list[dict]:
    _spend()
    r = await _client().get(path, params={"q": q, "limit": limit})
    r.raise_for_status()
    body = r.json()
    if isinstance(body, dict) and body.get("error"):
        raise DeezerError(str(body["error"]))
    return [x for x in (body.get("data") or []) if isinstance(x, dict)]


def _cover(album: dict) -> Optional[str]:
    return album.get("cover_big") or album.get("cover_xl") or album.get("cover_medium")


async def search_tracks(q: str, limit: int = 15) -> list[dict]:
    """[{title, artist, album, albumType, durationMs, rank, cover}] most relevant first."""
    out = []
    for x in await _search("/search", q, limit):
        artist, album = x.get("artist") or {}, x.get("album") or {}
        if not x.get("title") or not artist.get("name"):
            continue
        out.append({"title": x["title"], "artist": artist["name"], "album": album.get("title"),
                    "durationMs": int(x["duration"]) * 1000 if x.get("duration") else None,
                    "rank": int(x.get("rank") or 0), "cover": _cover(album)})
    return out


async def search_artists(q: str, limit: int = 10) -> list[dict]:
    """[{name, fans, picture}] in Deezer's order (relevance, which favours exact names)."""
    return [{"name": x["name"], "fans": int(x.get("nb_fan") or 0),
             "picture": x.get("picture_big") or x.get("picture_xl") or x.get("picture_medium")}
            for x in await _search("/search/artist", q, limit) if x.get("name")]


async def search_albums(q: str, limit: int = 10) -> list[dict]:
    """[{title, artist, recordType, cover}] most relevant first."""
    out = []
    for x in await _search("/search/album", q, limit):
        artist = x.get("artist") or {}
        if x.get("title") and artist.get("name"):
            out.append({"title": x["title"], "artist": artist["name"], "recordType": x.get("record_type"),
                        "cover": _cover(x)})
    return out
