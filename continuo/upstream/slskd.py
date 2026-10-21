# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""slskd, used to surface live Soulseek progress in the pipeline. Transfers are keyed by
Soulseek user + filename, not by album, so a request is matched to transfers heuristically:
artist AND album title in the path (lidseeker matched artist only, so two requests from one
artist collided), falling back to artist only when nothing matches both."""
import asyncio
import time
from typing import Optional

import httpx

from .. import config
from ..requests.normalise import normalise, strip_edition
from . import http

_TRANSFERS_TTL = 5.0
_transfers_cache: tuple[float, Optional[list]] = (0.0, None)
_transfers_lock = asyncio.Lock()


def enabled() -> bool:
    return bool(config.settings.slskd_api_key)


def _client() -> httpx.AsyncClient:
    s = config.settings
    return http.client("slskd", base_url=f"{s.slskd_url}/api/v0",
                       headers={"X-API-Key": s.slskd_api_key}, timeout=10.0)


async def _downloads() -> Optional[list]:
    """Current slskd downloads, cached briefly (one fetch serves every request row). None =
    unreachable."""
    global _transfers_cache
    async with _transfers_lock:
        if time.monotonic() - _transfers_cache[0] < _TRANSFERS_TTL:
            return _transfers_cache[1]
        try:
            r = await _client().get("/transfers/downloads")
            r.raise_for_status()
            users = r.json()
        except (httpx.HTTPError, ValueError):
            users = None
        _transfers_cache = (time.monotonic(), users)
        return users


def _is_active(state: str) -> bool:
    s = (state or "").lower()
    return "completed" not in s and "cancelled" not in s


def _is_done(state: str) -> bool:
    s = (state or "").lower()
    return "completed" in s and "succeeded" in s


def _aggregate(files: list[tuple[str, dict]]) -> Optional[dict]:
    if not files:
        return None
    size_total = bytes_done = files_done = 0
    active, username = False, None
    for user, f in files:
        size_total += f.get("size", 0) or 0
        bytes_done += f.get("bytesTransferred", 0) or 0
        state = f.get("state", "")
        if _is_done(state):
            files_done += 1
        if _is_active(state):
            active, username = True, user
    return {
        "active": active,
        "filesTotal": len(files),
        "filesDone": files_done,
        "percent": round(100 * bytes_done / size_total, 1) if size_total else 0.0,
        "username": username,
    }


async def album_progress(artist: Optional[str], album: Optional[str]) -> Optional[dict]:
    """Progress of in-flight slskd downloads for this album, or None (unconfigured,
    unreachable, or nothing matches)."""
    if not enabled() or not artist:
        return None
    artist_n = normalise(artist)
    album_n = normalise(strip_edition(album))
    if not artist_n:
        return None
    users = await _downloads()
    if users is None:
        return None
    both, artist_only = [], []
    for user in users:
        for directory in user.get("directories", []):
            for f in directory.get("files", []):
                path = normalise(f.get("filename", ""))
                if artist_n not in path:
                    continue
                artist_only.append((user.get("username"), f))
                if album_n and album_n in path:
                    both.append((user.get("username"), f))
    return _aggregate(both or artist_only)


async def delete_searches_for(artist: Optional[str], album: Optional[str] = None) -> int:
    """Remove slskd search records for this album (Soularr runs with delete_searches=False, so
    they pile up). Best-effort, never raises."""
    if not enabled() or not artist:
        return 0
    artist_n, album_n = normalise(artist), normalise(strip_edition(album))
    if not artist_n:
        return 0
    deleted = 0
    try:
        c = _client()
        r = await c.get("/searches")
        r.raise_for_status()
        for s in r.json():
            text = normalise(s.get("searchText", ""))
            if artist_n not in text or (album_n and album_n not in text):
                continue
            sid = s.get("id")
            if not sid:
                continue
            try:
                d = await c.delete(f"/searches/{sid}")
                if d.status_code < 300:
                    deleted += 1
            except httpx.HTTPError:
                continue
    except (httpx.HTTPError, ValueError):
        return deleted
    return deleted


async def ping() -> bool:
    try:
        r = await _client().get("/application", timeout=5.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
