"""Each account's own Navidrome library (#27): created at <NAVIDROME_LIBRARIES_ROOT>/<user>
(the hardlink folder library/links keeps), and the account given access to it alone, so every
Subsonic app shows that account only its albums. Admins see every library regardless."""
import asyncio
import logging
import os

from .. import config
from ..db import account_libraries as acc_db
from ..db import library as lib_db
from ..db import members as members_db
from ..db import users as users_db
from ..upstream import navidrome
from ..workers import index
from . import links

log = logging.getLogger("continuo")


async def navidrome_user(name: str) -> dict | None:
    users, _ = await navidrome.native_get("user")
    return next((u for u in users or [] if (u.get("userName") or "").lower() == name.lower()), None)


def navidrome_path(user: dict) -> str:
    """The account's library folder as Navidrome sees it."""
    return config.settings.navidrome_libraries_root.rstrip("/") + "/" + os.path.basename(
        links.library_root("", users_db.navidrome_name(user)))


async def ensure_library(user: dict) -> int:
    """The account's Navidrome library id, creating the library (and its folder) if needed."""
    known = acc_db.get(user["id"])
    if known:
        return known["navidrome_library_id"]
    store, libs = links.roots()
    os.makedirs(links.library_root(libs, users_db.navidrome_name(user)), exist_ok=True)
    path = navidrome_path(user)
    existing, _ = await navidrome.native_get("library")
    lib = next((x for x in existing or [] if x.get("path") == path), None)
    if lib is None:
        lib = await navidrome.native_send("POST", "library", {"name": users_db.navidrome_name(user), "path": path})
        log.info("libraries: created Navidrome library %s for %s at %s", lib.get("id"), user["username"], path)
    nd_user = await navidrome_user(users_db.navidrome_name(user))
    acc_db.set(user["id"], int(lib["id"]), nd_user.get("id") if nd_user else None)
    return int(lib["id"])


async def _grant(user: dict, library_ids: list[int]) -> None:
    nd_user = await navidrome_user(users_db.navidrome_name(user))
    if nd_user is None:
        raise LookupError(f"no Navidrome user {users_db.navidrome_name(user)!r}")
    await navidrome.native_send("PUT", f"user/{nd_user['id']}/library", {"libraryIds": library_ids})


async def grant_only_own(user: dict) -> None:
    """Give the account's Navidrome user access to its own library alone."""
    lib_id = await ensure_library(user)
    await _grant(user, [lib_id])
    log.info("libraries: %s now sees only library %s", user["username"], lib_id)


async def grant_store_and_own(user: dict) -> None:
    """Both libraries, while carrying ratings and playlists over: the account has to reach its
    new library's songs to rate them and put them in its playlists. grant_only_own ends it."""
    lib_id = await ensure_library(user)
    await _grant(user, [config.settings.store_library_id, lib_id])
    log.info("libraries: %s sees the store and library %s while carrying over", user["username"], lib_id)


async def scan_until(lib_id: int, expected: int, timeout_s: float = 1800, poll_s: float = 10) -> int:
    """Have Navidrome scan, then wait until the library holds [expected] albums (or time runs
    out). Returns how many it holds."""
    try:
        await navidrome.start_scan()
    except Exception as e:  # noqa: BLE001 — the watcher scans too
        log.warning("libraries: scan didn't start: %s", e)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    have = 0
    while True:
        await index.refresh()
        have = lib_db.count_in_library(lib_id)
        if have >= expected or loop.time() >= deadline:
            return have
        await asyncio.sleep(poll_s)


async def init(user: dict, everything: bool) -> dict:
    """Create the account's library and fill it: with everything in the store (--all), or with
    what it already has. Access is not changed here (grant_only_own does that)."""
    lib_id = await ensure_library(user)
    added = 0
    if everything:
        folders = sorted({a["folder"] for a in lib_db.all_albums() if a.get("folder")})
        added = members_db.add_many(user["id"], folders, "migration")
    store, libs = links.roots()
    stats = links.sync_user(user, store, libs)
    mine = members_db.folders(user["id"])
    # What Navidrome should index there: as many albums as the store has in those folders (one
    # folder can hold two albums).
    expected = sum(1 for a in lib_db.all_albums() if a.get("folder") in mine)
    return {"library": lib_id, "added": added, "albums": len(mine), "expected": expected,
            "linked": stats.linked, "relinked": stats.relinked, "removed": stats.removed}


async def revert(user: dict) -> None:
    """Back to the shared store: the account sees the store library again and continuo stops
    treating it as having its own (its library folder and list stay, for another go)."""
    nd_user = await navidrome_user(users_db.navidrome_name(user))
    if nd_user is None:
        raise LookupError(f"no Navidrome user {users_db.navidrome_name(user)!r}")
    await navidrome.native_send("PUT", f"user/{nd_user['id']}/library",
                                {"libraryIds": [config.settings.store_library_id]})
    acc_db.delete(user["id"])
    log.info("libraries: %s is back on the shared store library", user["username"])
