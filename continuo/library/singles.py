"""Single songs in the store (#24): <MUSIC_DIR>/<SINGLES_FOLDER>/<Album Artist>/<Album>/.

Lidarr never sees them (the singles folder isn't one of its artist folders), so to Lidarr an
album is whole or absent and requesting it is an ordinary whole-album download. To everyone
else an album made only of singles is partial: "1 of N", the album still requestable. When the
whole album arrives its singles are deleted, and every account that had one gets the album."""
import logging
import os
import shutil
from typing import Optional

from .. import config
from ..db import members as members_db
from ..db import requests as req_db
from ..db import users as users_db
from . import links

log = logging.getLogger("continuo")


def root() -> str:
    return config.settings.singles_folder


def is_singles(folder: Optional[str]) -> bool:
    return bool(folder) and (folder == root() or folder.startswith(root() + "/"))


def whole(albums: list[dict]) -> list[dict]:
    """Only real albums: what request matching and "already in the library" go by."""
    return [a for a in albums if not is_singles(a.get("folder"))]


def _filed() -> list[dict]:
    return [r for r in req_db.list_requests(statuses=("available", "scanning"), kind="track") if r.get("file_path")]


class Singles:
    """What the store's single songs are, for availability: per singles folder the album's
    track count, and the recordings filed."""

    def __init__(self) -> None:
        self.expected: dict[str, int] = {}
        self.recordings: set[str] = set()
        for r in _filed():
            folder = r["file_path"].rsplit("/", 1)[0]
            n = (r.get("track") or {}).get("trackCount")
            if n:
                self.expected[folder] = max(self.expected.get(folder, 0), int(n))
            self.recordings.add(r["mb_release_group_id"])

    def availability(self, lib: dict, album_id: Optional[str]) -> dict:
        """A singles-only album: partial, and the album can still be requested."""
        have = int(lib.get("song_count") or 1)
        expected = max(self.expected.get(lib.get("folder") or "", 0), have)
        return {"state": "partial", "have": have, "expected": expected, "navidromeAlbumId": album_id,
                "requestable": True}


def absorb(release_group_id: Optional[str], album_folder: Optional[str]) -> int:
    """The whole album arrived (at [album_folder]): its singles go, and whoever had one has the
    album. Returns how many songs were dropped."""
    if not release_group_id or not album_folder or is_singles(album_folder):
        return 0
    from .cache import Unavailable, music_dir
    try:
        store = music_dir()
    except Unavailable:
        return 0
    rows = [r for r in _filed() if r.get("album_release_group_id") == release_group_id]
    folders = {r["file_path"].rsplit("/", 1)[0] for r in rows}
    for r in rows:
        path = os.path.realpath(os.path.join(store, r["file_path"]))
        if path.startswith(os.path.join(store, root()) + os.sep) and os.path.isfile(path):
            os.remove(path)
        req_db.update(r["id"], file_path=None)
        req_db.add_event(r["id"], "absorbed", {"album": album_folder})
    for folder in folders:
        for uid in members_db.holders(folder):
            members_db.add(uid, album_folder, "request")
            members_db.remove(uid, folder)
            user = users_db.get_by_id(uid)
            if user:
                try:
                    links.unlink_album(user, folder)
                    store_root, libs = links.roots()
                    links.sync_album(store_root, links.library_root(libs, users_db.navidrome_name(user)), album_folder)
                except (links.Unavailable, ValueError) as e:
                    log.warning("singles: %s's library not updated now (%s); the sync loop will", user["username"], e)
        path = os.path.realpath(os.path.join(store, folder))
        if path.startswith(os.path.join(store, root()) + os.sep) and os.path.isdir(path) and not any(
                f for f in os.listdir(path) if not f.startswith(".")):
            shutil.rmtree(path, ignore_errors=True)
            parent = os.path.dirname(path)
            if parent != os.path.join(store, root()) and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
    if rows:
        log.info("singles: %d song(s) of %s now come with the whole album", len(rows), album_folder)
    return len(rows)
