# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""MusicBrainz: track search and tracklists. One shared client with the required User-Agent,
a ~1 req/s pacer (MusicBrainz's rate limit), and caches (lidseeker opened a fresh client per call
and cached nothing)."""
import asyncio
import time
from typing import Optional

import httpx

from .. import config
from ..requests.normalise import track_key
from . import http
from .lidarr import _year, coverart_url

_MB_URL = "https://musicbrainz.org/ws/2"
_RG_TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}
_MIN_INTERVAL = 1.05
_SEARCH_TTL = 600.0

_pace_lock = asyncio.Lock()
_last_call = 0.0
_search_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_tracklist_cache: dict[str, list[dict]] = {}
_TRACKLIST_CACHE_MAX = 512


def _client() -> httpx.AsyncClient:
    return http.client("musicbrainz", base_url=_MB_URL, timeout=20.0,
                       headers={"User-Agent": config.settings.musicbrainz_user_agent,
                                "Accept": "application/json"})


async def _get(path: str, params: dict) -> dict:
    global _last_call
    async with _pace_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            r = await _client().get(path, params={**params, "fmt": "json"})
        finally:
            _last_call = time.monotonic()
    r.raise_for_status()
    return r.json()


def pick_release_group(rec: dict) -> Optional[dict]:
    """Best parent release-group for a recording: an official Album/EP without
    compilation/live secondary types, earliest first."""
    best, best_key = None, None
    for rel in rec.get("releases", []):
        rg = rel.get("release-group") or {}
        rg_id = rg.get("id")
        if not rg_id:
            continue
        primary = rg.get("primary-type") or ""
        rank = _RG_TYPE_RANK.get(primary, 9) + (5 if rg.get("secondary-types") else 0)
        rg_date = rel.get("date") or rg.get("first-release-date") or "9999"
        key = (rank, rg_date)
        if best_key is None or key < best_key:
            best_key, best = key, {"id": rg_id, "title": rg.get("title") or rel.get("title"),
                                   "primary": primary, "date": rg_date}
    return best


async def search_tracks(term: str, limit: int = 25) -> list[dict]:
    """Recordings matching `term`, each pointing at the album that would fulfil a request.
    Raises httpx.HTTPError on failure (the caller reports the catalogue as degraded)."""
    key = (term.strip().casefold(), limit)
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < _SEARCH_TTL:
        return hit[1]
    data = await _get("/recording", {"query": term, "limit": limit})
    out: list[dict] = []
    seen: set[str] = set()
    for rec in data.get("recordings", []):
        rg = pick_release_group(rec)
        if not rg:
            continue
        credit = rec.get("artist-credit") or []
        artist = credit[0].get("name") if credit else None
        dedupe = track_key(artist, rec.get("title"))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append({
            "kind": "track",
            "musicBrainzRecordingId": rec.get("id"),
            "title": rec.get("title"),
            "artist": artist,
            "durationMs": rec.get("length"),
            "album": {
                "title": rg.get("title"),
                "musicBrainzReleaseGroupId": rg["id"],
                "year": _year(rg.get("date")),
                "albumType": rg.get("primary"),
            },
            "imageUrl": coverart_url(rg["id"]),
        })
    if len(_search_cache) > 256:
        _search_cache.clear()
    _search_cache[key] = (time.monotonic(), out)
    return out


def _pick_release(releases: list[dict]) -> Optional[str]:
    if not releases:
        return None
    return min(releases, key=lambda rel: (0 if rel.get("status") == "Official" else 1,
                                         rel.get("date") or "9999")).get("id")


async def album_tracks(release_group_mbid: str, release_mbid: Optional[str] = None) -> list[dict]:
    """Tracklist of one canonical official release of the group (or the given release).
    A release's tracklist never changes, so it's cached for the process life (bounded)."""
    cache_key = release_mbid or release_group_mbid
    if cache_key in _tracklist_cache:
        return _tracklist_cache[cache_key]
    try:
        if not release_mbid:
            rg = await _get(f"/release-group/{release_group_mbid}", {"inc": "releases"})
            release_mbid = _pick_release(rg.get("releases", []))
            if not release_mbid:
                return []
        rel = await _get(f"/release/{release_mbid}", {"inc": "recordings"})
    except (httpx.HTTPError, ValueError):
        return []
    out: list[dict] = []
    for medium in rel.get("media", []):
        disc = medium.get("position") or 1
        for t in medium.get("tracks", []):
            recording = t.get("recording") or {}
            title = t.get("title") or recording.get("title")
            if not title:
                continue
            length = t.get("length") or recording.get("length")
            out.append({
                "position": t.get("position") or len(out) + 1,
                "title": title,
                "durationMs": int(length) if length else None,
                "discNumber": disc,
                "musicBrainzRecordingId": recording.get("id"),
            })
    if len(_tracklist_cache) >= _TRACKLIST_CACHE_MAX:
        del _tracklist_cache[next(iter(_tracklist_cache))]
    _tracklist_cache[cache_key] = out
    return out


async def release_group_for_title(artist_mbid: str, title: str) -> Optional[str]:
    """The release group of this artist's release called [title], via MusicBrainz's release
    search, which also knows translated and romanised release titles ("Windswept Adan" is a
    release of アダンの風). None when nothing scores high enough."""
    q = title.replace("\\", " ").replace('"', " ").strip()
    if not q:
        return None
    try:
        data = await _get("/release", {"query": f'release:"{q}" AND arid:{artist_mbid}', "limit": 5})
    except (httpx.HTTPError, ValueError):
        return None
    for rel in data.get("releases") or []:
        rg = (rel.get("release-group") or {}).get("id")
        if rg and int(rel.get("score") or 0) >= 90:
            return rg
    return None
