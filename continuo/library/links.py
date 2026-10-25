"""Per-user library folders (#27): each account's library is a tree of hardlinks into the store.

<LIBRARIES_DIR>/<Navidrome username>/<Artist>/<Album>/<file> is the same file as
<MUSIC_DIR>/<Artist>/<Album>/<file>: no extra space, and a tag written once shows everywhere.
library_members says which album folders an account has; this module makes the folders match,
the way music-tagscrub keeps the Soulseek share: a library file whose store file was replaced
(tagscrub scrubs tags and adds SEEKTABLEs by copy + rename, a new inode) is relinked, files the
store no longer has are dropped, and anything the account doesn't have is removed. Paths stay
the same, so Navidrome keeps its ids (and the account's ratings) across a relink."""
import logging
import os
import shutil
import time
from dataclasses import dataclass

from .. import config
from ..db import members as members_db
from ..db import users as users_db
from . import cache

log = logging.getLogger("continuo")


class Unavailable(Exception):
    """LIBRARIES_DIR isn't set, or isn't on the store's filesystem and mount."""


@dataclass
class Stats:
    linked: int = 0
    relinked: int = 0
    removed: int = 0

    def __iadd__(self, other: "Stats") -> "Stats":
        self.linked += other.linked
        self.relinked += other.relinked
        self.removed += other.removed
        return self


def roots() -> tuple[str, str]:
    """(store, libraries), both real paths; hardlinks need them on one filesystem and mount."""
    store = cache.music_dir()
    libs = config.settings.libraries_dir
    if not libs:
        raise Unavailable("LIBRARIES_DIR is not set")
    os.makedirs(libs, exist_ok=True)
    libs = os.path.realpath(libs)
    if os.stat(libs).st_dev != os.stat(store).st_dev:
        raise Unavailable("LIBRARIES_DIR and MUSIC_DIR are on different filesystems: hardlinks can't cross")
    return store, libs


def library_root(libs: str, username: str) -> str:
    safe = username.replace("/", "_").replace("\\", "_").strip(".") or "_"
    return os.path.join(libs, safe)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _album_paths(store: str, root: str, folder: str) -> tuple[str, str]:
    src = os.path.realpath(os.path.join(store, folder))
    dst = os.path.normpath(os.path.join(root, folder))
    if not folder or folder.startswith(("/", "..")) or not _inside(src, store) or src == store \
            or not _inside(dst, root) or dst == root or _inside(src, os.path.join(store, cache.CACHE_FOLDER)):
        raise ValueError(f"unsafe album folder {folder!r}")
    return src, dst


def sync_album(store: str, root: str, folder: str) -> Stats:
    """Make root/folder an exact hardlink mirror of store/folder (gone from the store = removed)."""
    src, dst = _album_paths(store, root, folder)
    stats = Stats()
    if not os.path.isdir(src):
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
            stats.removed += 1
        return stats
    want: set[str] = set()
    for dirpath, _dirs, files in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        for name in files:
            if name.startswith(".") and name.endswith((".tagscrub", ".seektable")):
                continue  # tagscrub's work in progress
            rel = os.path.normpath(os.path.join(rel_dir, name))
            want.add(rel)
            s, d = os.path.join(src, rel), os.path.join(dst, rel)
            try:
                st = os.stat(s)
                if os.path.exists(d) and os.stat(d).st_ino == st.st_ino:
                    continue
                os.makedirs(os.path.dirname(d), exist_ok=True)
                fresh = not os.path.exists(d)
                tmp = os.path.join(os.path.dirname(d), f".{name}.link")
                if os.path.exists(tmp):
                    os.remove(tmp)
                os.link(s, tmp)
                os.replace(tmp, d)
                if fresh:
                    stats.linked += 1
                else:
                    stats.relinked += 1
            except FileNotFoundError:
                continue  # replaced while we looked: the next sync catches it
    for dirpath, _dirs, files in os.walk(dst, topdown=False):
        rel_dir = os.path.relpath(dirpath, dst)
        for name in files:
            if os.path.normpath(os.path.join(rel_dir, name)) not in want:
                os.remove(os.path.join(dirpath, name))
                stats.removed += 1
        if dirpath != dst and not os.listdir(dirpath):
            os.rmdir(dirpath)
    return stats


def _prune_empty(path: str, stop: str) -> None:
    while _inside(path, stop) and path != stop and os.path.isdir(path) and not os.listdir(path):
        os.rmdir(path)
        path = os.path.dirname(path)


def sync_user(user: dict, store: str, libs: str) -> Stats:
    """Make an account's library folder hold exactly its member albums."""
    root = library_root(libs, users_db.navidrome_name(user))
    os.makedirs(root, exist_ok=True)
    wanted = members_db.folders(user["id"])
    stats = Stats()
    for folder in sorted(wanted):
        try:
            stats += sync_album(store, root, folder)
        except ValueError as e:
            log.warning("library %s: %s", user["username"], e)
    # Anything not under a member folder goes (a removed album, a stray file).
    keep = {os.path.normpath(os.path.join(root, f)) for f in wanted}
    for dirpath, dirs, files in os.walk(root, topdown=True):
        if dirpath in keep:
            dirs[:] = []                      # a member album: sync_album already made it exact
            continue
        for name in files:                    # a stray file above or beside the album folders
            os.remove(os.path.join(dirpath, name))
            stats.removed += 1
        descend = []
        for d in dirs:
            full = os.path.join(dirpath, d)
            if any(_inside(k, full) for k in keep):
                descend.append(d)             # leads to a member album (e.g. its artist folder)
            else:
                shutil.rmtree(full, ignore_errors=True)
                stats.removed += 1
        dirs[:] = descend
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if dirpath != root:
            _prune_empty(dirpath, root)
    return stats


def unlink_album(user: dict, folder: str) -> None:
    """Take one album out of an account's library folder (its membership already removed)."""
    store, libs = roots()
    root = library_root(libs, users_db.navidrome_name(user))
    _src, dst = _album_paths(store, root, folder)
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    _prune_empty(os.path.dirname(dst), root)


# An album must have sat in the store this long before it counts as unclaimed: a request's own
# claim (workers/scan) comes first.
UNCLAIMED_AFTER_S = 600


def adopt_unclaimed(store: str) -> int:
    """Albums in the store that no library has and no live request is for (they came in through
    Lidarr's own UI, or were copied in) go to LIBRARY_UNCLAIMED_TO's accounts; otherwise nobody
    would ever see them. Single songs are left to their requests."""
    from ..db import account_libraries as acc_db
    from ..db import library as lib_db
    from ..db import requests as req_db
    from . import singles
    names = config.settings.library_unclaimed_to
    takers = [u for u in (users_db.get(n) for n in names) if u and acc_db.get(u["id"])]
    if not takers:
        return 0
    held, wanted = members_db.held(), set(req_db.active_by_release_group())
    now = time.time()
    adopted = 0
    for a in singles.whole(lib_db.all_albums()):
        folder = a.get("folder")
        if not folder or folder in held or a.get("mbz_release_group_id") in wanted:
            continue
        path = os.path.join(store, folder)
        try:
            if now - os.stat(path).st_mtime < UNCLAIMED_AFTER_S:
                continue
        except OSError:
            continue                      # gone from disk (removed, renamed): nothing to adopt
        for u in takers:
            members_db.add(u["id"], folder, "unclaimed")
        held.add(folder)
        adopted += 1
        log.info("libraries: %r came in outside a request; now in %s's library", folder,
                 ", ".join(u["username"] for u in takers))
    return adopted


async def tick() -> None:
    """The library sync loop: albums nobody has go to LIBRARY_UNCLAIMED_TO, then every account
    with members gets its folder brought up to date."""
    try:
        store, libs = roots()
    except (Unavailable, cache.Unavailable):
        return
    adopt_unclaimed(store)
    for uid in members_db.user_ids():
        user = users_db.get_by_id(uid)
        if not user:
            continue
        stats = sync_user(user, store, libs)
        if stats.linked or stats.relinked or stats.removed:
            log.info("library %s: %d linked, %d relinked, %d removed", user["username"],
                     stats.linked, stats.relinked, stats.removed)
