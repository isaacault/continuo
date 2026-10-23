"""Removing an album from the library, and putting it back (issue #23).

Remove: unmonitor the album in Lidarr (so it is never downloaded again), move its folder into the
media cache with one rename, then let Lidarr and Navidrome rescan. Restore is the same in
reverse, re-monitoring only what was monitored. Both refuse anything that isn't one album's own
artist/album folder under MUSIC_DIR."""
import asyncio
import logging
import os
import posixpath
import time
from datetime import timedelta

import httpx

from .. import config
from ..db import cache as cache_db
from ..db import library as lib_db
from ..db.conn import parse_ts
from ..errors import ApiError
from ..upstream import lidarr, navidrome
from . import cache

log = logging.getLogger("continuo")

# One move at a time: two removals of albums in one artist folder, or a restore racing a
# removal, must not interleave.
_lock = asyncio.Lock()


def _music_dir() -> str:
    try:
        return cache.music_dir()
    except cache.Unavailable:
        raise ApiError(503, "removal_unavailable", "Removing albums isn't set up on this server (MUSIC_DIR).") from None


def _album_dir(root: str, folder: str | None) -> tuple[str, str]:
    """(absolute path, normalised relative path) of an album folder that is safe to move."""
    if not folder:
        raise ApiError(409, "no_folder", "continuo doesn't know where this album's files are yet. "
                                         "Try again after the next library refresh.")
    rel = posixpath.normpath(folder.replace("\\", "/")).lstrip("/")
    parts = rel.split("/")
    if rel.startswith("..") or len(parts) < 2 or parts[0] == cache.CACHE_FOLDER:
        raise ApiError(409, "unsafe_folder", "This album's files aren't in an artist/album folder of "
                                             "their own, so they can't be removed safely.")
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) or not os.path.isdir(path):
        raise ApiError(409, "missing_folder", "The album's folder isn't where the library says it is.")
    return path, rel


def _out(item: dict) -> dict:
    created = parse_ts(item["created_at"])
    until = created + timedelta(days=config.settings.removed_max_days) if created else None
    return {
        "id": item["id"], "kind": item["kind"], "title": item["title"], "artist": item["artist"],
        "removedAt": item["created_at"], "removedBy": item["removed_by"],
        "restorableUntil": until.isoformat(timespec="seconds") if until else None,
        "bytes": item["bytes"],
        "ids": {"musicBrainzReleaseGroupId": item["mb_release_group_id"],
                "navidromeAlbumId": item["navidrome_album_id"]},
    }


async def _rescan(lidarr_artist_id: int | None) -> None:
    """Best effort: Lidarr notices the files moved, Navidrome drops or re-adds the album."""
    if lidarr_artist_id:
        try:
            await lidarr.run_command("RefreshArtist", wait=False, artistId=lidarr_artist_id)
        except httpx.HTTPError as e:
            log.warning("removal: Lidarr refresh failed: %s", e)
    try:
        await navidrome.start_scan()
    except Exception as e:  # noqa: BLE001 — the scan loop catches up anyway
        log.warning("removal: Navidrome scan failed: %s", e)


async def remove(navidrome_id: str, by_user: str) -> dict:
    album = lib_db.get(navidrome_id)
    if album is None:
        raise ApiError(404, "not_found", "That album isn't in the library.")
    root = _music_dir()
    async with _lock:
        path, rel = _album_dir(root, album.get("folder"))
        if any(a["navidrome_id"] != navidrome_id for a in lib_db.by_folder(album["folder"])):
            raise ApiError(409, "shared_folder", "Another album shares this folder, so it can't be removed on its own.")
        base = cache.cache_dir()

        # Lidarr first: an album left monitored would only be downloaded again.
        try:
            lid = await lidarr.find_album_by_foreign_id(album.get("mbz_release_group_id"))
        except httpx.HTTPError as e:
            raise ApiError(502, "lidarr_unavailable", "Lidarr isn't answering, so the album wasn't removed.",
                           retryable=True) from e
        was_monitored = bool(lid and lid.get("monitored"))
        if was_monitored:
            await lidarr.set_album_monitored([lid["id"]], False)

        cache_rel = f"removed-{int(time.time())}-{navidrome_id}/{posixpath.basename(rel)}"
        dest = os.path.join(base, cache_rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=False)
            os.rename(path, dest)
        except OSError as e:
            if was_monitored:
                await lidarr.set_album_monitored([lid["id"]], True)
            log.error("removal: moving %r into the cache failed: %s", rel, e)
            raise ApiError(500, "move_failed", "The album's files couldn't be moved, so nothing was removed.") from e

        item = cache_db.add({
            "kind": "album", "origin": "removed", "title": album["name"], "artist": album.get("artist"),
            "mb_release_group_id": album.get("mbz_release_group_id"), "mb_release_id": album.get("mbz_album_id"),
            "navidrome_album_id": navidrome_id, "library_path": rel, "cache_path": cache_rel,
            "lidarr_album_id": lid["id"] if lid else None, "lidarr_was_monitored": int(was_monitored),
            "bytes": cache.size_of(dest), "removed_by": by_user,
        })
        lib_db.delete(navidrome_id)
    log.info("removal: %s removed %r by %s (cache item %s)", by_user, album["name"], album.get("artist"), item["id"])
    await _rescan(lid.get("artistId") if lid else None)
    cache.evict()
    return _out(item)


def removed() -> list[dict]:
    return [_out(i) for i in cache_db.list_items("removed")]


async def restore(item_id: int) -> dict:
    item = cache_db.get(item_id)
    if item is None or item["origin"] != "removed":
        raise ApiError(404, "not_found", "Nothing to restore with that id (it may have been dropped from the cache).")
    root = _music_dir()
    async with _lock:
        base = cache.cache_dir()
        src = os.path.realpath(os.path.join(base, item["cache_path"]))
        dest = os.path.realpath(os.path.join(root, item["library_path"]))
        if not src.startswith(base + os.sep) or not os.path.isdir(src):
            raise ApiError(410, "gone", "The album's files are no longer in the cache.")
        if not dest.startswith(root + os.sep) or dest.startswith(base + os.sep):
            raise ApiError(409, "unsafe_folder", "The album's original folder isn't in the library.")
        if os.path.exists(dest):
            raise ApiError(409, "path_taken", "Something new is in the album's old folder, so it wasn't restored.")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src, dest)
        try:
            os.rmdir(os.path.dirname(src))  # the removed-<ts>-<id> wrapper, now empty
        except OSError:
            pass
        artist_id = None
        if item["lidarr_album_id"]:
            try:
                if item["lidarr_was_monitored"]:
                    await lidarr.set_album_monitored([item["lidarr_album_id"]], True)
                artist_id = (await lidarr.get_album(item["lidarr_album_id"], fresh=True)).get("artistId")
            except httpx.HTTPError as e:
                log.warning("restore: Lidarr didn't take the album back (%s); re-monitor it there", e)
        cache_db.delete(item_id)
    log.info("restore: %r by %s is back in the library", item["title"], item["artist"])
    await _rescan(artist_id)
    return {"restored": True, "title": item["title"], "artist": item["artist"]}
