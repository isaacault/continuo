"""What "in your library" means for one account once it has its own library (#27).

An album in the store is either in the account's library (a member: it opens with the album id
in the account's own Navidrome library) or merely *on the server* (another account has it):
state "on_server", which the app offers as Add, instantly, with no download. Accounts without a
library of their own (none yet, or LIBRARIES_DIR off) see the store as before."""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import config
from ..db import account_libraries as acc_db
from ..db import cache as cache_db
from ..db import library as lib_db
from ..db import members as members_db
from ..db import removals as removals_db
from ..db import users as users_db
from ..db.conn import parse_ts
from ..errors import ApiError
from ..upstream import navidrome
from . import cache, links

log = logging.getLogger("continuo")


class Ownership:
    def __init__(self, user_id: Optional[int]):
        acc = acc_db.get(user_id) if user_id is not None and config.settings.libraries_dir else None
        self.enabled = acc is not None
        self.user_id = user_id
        self.library_id = acc["navidrome_library_id"] if acc else None
        self.mine = members_db.folders(user_id) if acc else set()
        self.ids = lib_db.folders_in_library(acc["navidrome_library_id"]) if acc else {}

    def view(self, lib: dict, availability: dict) -> dict:
        """[availability] (built from the store album [lib]) as this account sees it."""
        if not self.enabled:
            return availability
        folder = lib.get("folder")
        if folder and folder in self.mine:
            return {**availability, "navidromeAlbumId": self.ids.get(folder)}
        return {"state": "on_server", "storeAlbumId": lib["navidrome_id"],
                **({"trackCount": lib["song_count"]} if lib.get("song_count") else {})}


def has_own_library(user_id: Optional[int]) -> bool:
    return user_id is not None and bool(config.settings.libraries_dir) and acc_db.get(user_id) is not None


def _album(navidrome_id: str) -> dict:
    a = lib_db.get(navidrome_id)
    if a is None or not a.get("folder"):
        raise ApiError(404, "not_found", "That album isn't on the server.")
    return a


async def add(user: dict, store_album_id: str) -> dict:
    """Put an album already on the server into the account's library: instant, no download."""
    own = Ownership(user["id"])
    if not own.enabled:
        raise ApiError(409, "no_library", "This account has no library of its own yet.")
    a = _album(store_album_id)
    members_db.add(user["id"], a["folder"], "add")
    links.sync_album(*_paths(user), a["folder"])
    await _scan()
    log.info("libraries: %s added %r", user["username"], a["name"])
    return {"added": True, "title": a["name"], "artist": a.get("artist")}


def _out(r: dict) -> dict:
    """The app's removed-album shape (as removal._out), for the account's own removals."""
    created = parse_ts(r["removed_at"])
    until = created + timedelta(days=config.settings.removed_max_days) if created else None
    return {"id": r["id"], "kind": "album", "title": r["title"], "artist": r["artist"],
            "removedAt": r["removed_at"], "removedBy": None,
            "restorableUntil": until.isoformat(timespec="seconds") if until else None, "bytes": 0,
            "ids": {"musicBrainzReleaseGroupId": r["mb_release_group_id"],
                    "navidromeAlbumId": r["navidrome_album_id"]}}


def _restorable(r: dict) -> bool:
    if os.path.isdir(os.path.join(cache.music_dir(), r["folder"])):
        return True                       # still in the store: someone else has it
    return bool(r.get("cache_item_id")) and cache_db.get(r["cache_item_id"]) is not None


async def remove(user: dict, navidrome_id: str) -> dict:
    """Take an album out of the account's library. Other libraries keep theirs; when no library
    has it any more the store copy goes to the media cache (#23) and Lidarr stops monitoring it.
    Either way it shows in the account's Recently removed and can be put back."""
    from . import removal
    a = lib_db.get(navidrome_id)
    folder = a.get("folder") if a else None
    if not folder or not members_db.remove(user["id"], folder):
        raise ApiError(404, "not_found", "That album isn't in your library.")
    links.unlink_album(user, folder)
    store_album = next(iter(lib_db.by_folder(folder)), None)
    cache_item_id = None
    if not members_db.holders(folder) and store_album:
        try:
            cached = await removal.remove(store_album["navidrome_id"], user["username"])
            cache_item_id = cached["id"]
        except ApiError:
            members_db.add(user["id"], folder, "add")     # put it back as it was
            links.sync_album(*_paths(user), folder)
            raise
    r = removals_db.add({"user_id": user["id"], "folder": folder, "title": a["name"], "artist": a.get("artist"),
                         "mb_release_group_id": a.get("mbz_release_group_id"), "navidrome_album_id": navidrome_id,
                         "cache_item_id": cache_item_id})
    await _scan()
    log.info("libraries: %s removed %r%s", user["username"], a["name"], " (last copy: cached)" if cache_item_id else "")
    return _out(r)


def removed(user: dict) -> list[dict]:
    """The account's removals still restorable, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.settings.removed_max_days)).isoformat(timespec="seconds")
    removals_db.delete_older_than(cutoff)
    return [_out(r) for r in removals_db.for_user(user["id"]) if _restorable(r)]


async def restore(user: dict, item_id: int) -> dict:
    """Put a removed album back in the account's library (out of the cache first when it was
    the last copy)."""
    from . import removal
    r = removals_db.get(item_id)
    if r is None or r["user_id"] != user["id"]:
        raise ApiError(404, "not_found", "Nothing of yours to restore with that id.")
    if not os.path.isdir(os.path.join(cache.music_dir(), r["folder"])):
        if not r.get("cache_item_id") or cache_db.get(r["cache_item_id"]) is None:
            raise ApiError(410, "gone", "The album's files are no longer on the server.")
        await removal.restore(r["cache_item_id"])
    members_db.add(user["id"], r["folder"], "add")
    links.sync_album(*_paths(user), r["folder"])
    removals_db.delete(item_id)
    await _scan()
    return {"restored": True, "title": r["title"], "artist": r["artist"]}


def _paths(user: dict) -> tuple[str, str]:
    store, libs = links.roots()
    return store, links.library_root(libs, users_db.navidrome_name(user))


async def claim(user_id: Optional[int], store_album: dict) -> tuple[bool, Optional[str]]:
    """A requested album arrived in the store: into the requester's library. (True, the album id
    in the requester's library) once Navidrome has indexed it; (False, None) while it hasn't;
    (True, None) for an account without a library (the store id stands, as before)."""
    own = Ownership(user_id)
    if not own.enabled or not store_album.get("folder"):
        return True, None
    user = users_db.get_by_id(user_id)
    folder = store_album["folder"]
    if members_db.add(user_id, folder, "request"):
        links.sync_album(*_paths(user), folder)
        await _scan()
    mine = lib_db.folders_in_library(own.library_id).get(folder)
    return (True, mine) if mine else (False, None)


async def _scan() -> None:
    try:
        await navidrome.start_scan()
    except Exception as e:  # noqa: BLE001 — Navidrome's watcher picks it up anyway
        log.warning("libraries: scan didn't start: %s", e)
