"""Refresh library_albums from Navidrome: after scans and nightly.

Native API (preferred): album list with mbzAlbumId + mbzReleaseGroupId, plus one song per new or
changed album for its folder. If the native API isn't reachable (no service password and no
ExtAuth yet), fall back to Subsonic getAlbumList2: release MBIDs only, no folders, so matching
falls back to release/fuzzy strategies."""
import asyncio
import logging

import httpx

from ..db import library as lib_db
from ..requests.normalise import album_key
from ..upstream import navidrome
from ..upstream.navidrome import NavidromeError, relative_path

log = logging.getLogger("continuo")

_lock = asyncio.Lock()
last_mode: str | None = None


def _year(v) -> int | None:
    try:
        y = int(v or 0)
    except (TypeError, ValueError):
        return None
    return y or None


async def _native() -> list[dict]:
    known = lib_db.source_versions()
    albums = [a async for a in navidrome.iter_albums()]
    sem = asyncio.Semaphore(4)

    async def folder_for(a: dict) -> str | None:
        prev = known.get(a["id"])
        if prev and prev[0] == a.get("updatedAt") and prev[1]:
            return prev[1]
        async with sem:
            try:
                paths = await navidrome.album_song_paths(a["id"], limit=1000)
            except (httpx.HTTPError, NavidromeError, ValueError):
                return prev[1] if prev else None
        return navidrome.album_folder([relative_path(p) for p in paths])

    folders = await asyncio.gather(*(folder_for(a) for a in albums))
    out = []
    for a, folder in zip(albums, folders, strict=True):
        artist = a.get("albumArtist") or a.get("artist")
        out.append({
            "navidrome_id": a["id"], "name": a.get("name") or "", "artist": artist,
            "year": _year(a.get("maxYear") or a.get("minYear")),
            "song_count": a.get("songCount"), "mbz_album_id": a.get("mbzAlbumId") or None,
            "mbz_release_group_id": a.get("mbzReleaseGroupId") or None, "folder": folder or None,
            "norm_key": album_key(artist, a.get("name")),
            "source_updated_at": a.get("updatedAt"),
            "library_id": a.get("libraryId"),
        })
    return out


async def _subsonic(page: int = 500) -> list[dict]:
    out, offset = [], 0
    while True:
        body = await navidrome.subsonic("getAlbumList2", {
            "type": "alphabeticalByName", "size": page, "offset": offset})
        albums = (body.get("albumList2") or {}).get("album") or []
        for a in albums:
            out.append({
                "navidrome_id": a["id"], "name": a.get("name") or "", "artist": a.get("artist"),
                "year": _year(a.get("year")), "song_count": a.get("songCount"),
                "mbz_album_id": a.get("musicBrainzId") or None, "mbz_release_group_id": None,
                "folder": None, "norm_key": album_key(a.get("artist"), a.get("name")),
                "source_updated_at": None,
            })
        if len(albums) < page:
            return out
        offset += page


async def refresh() -> dict:
    """Rebuild the index. Returns {mode, albums, removed}."""
    global last_mode
    async with _lock:
        try:
            albums, mode = await _native(), "native"
        except (httpx.HTTPError, NavidromeError) as e:
            log.info("native Navidrome API unavailable (%s); indexing via Subsonic", e)
            albums, mode = await _subsonic(), "subsonic"
        lib_db.upsert_many(albums)
        removed = lib_db.delete_missing({a["navidrome_id"] for a in albums})
        last_mode = mode
        log.info("library index: %d albums (%s), %d removed", len(albums), mode, removed)
        return {"mode": mode, "albums": len(albums), "removed": removed}
