"""Giving the library's non-Latin artists their NAME_LOCALE name (フィッシュマンズ -> Fishmans), for
albums Lidarr downloaded only: an album counts as downloaded when Lidarr's history shows its files
imported from LIDARR_DOWNLOADS_PATH. The user's own copies are listed and left alone."""
import os
import posixpath
from dataclasses import dataclass, field

import httpx

from .. import config
from ..db import identity
from ..db import library as lib_db
from ..search import resolve
from ..upstream import lidarr
from . import cache, naming, tags


@dataclass
class Plan:
    artist: str
    name: str
    sort: str
    mbid: str
    retag: list[dict] = field(default_factory=list)     # library_albums rows Lidarr downloaded
    keep: list[dict] = field(default_factory=list)      # the user's own: never touched


def _folder_of(lidarr_path: str) -> str | None:
    """A Lidarr file path's album folder relative to the music root."""
    root = config.settings.root_folder_path.rstrip("/")
    p = lidarr_path.replace("\\", "/")
    return posixpath.dirname(p[len(root) + 1:]) if p.startswith(root + "/") else None


async def plan() -> tuple[list[Plan], list[str]]:
    """What --apply would do, and notes on artists it can't place."""
    by_artist: dict[str, list[dict]] = {}
    for a in lib_db.all_albums():
        if a.get("artist") and not naming.is_latin(a["artist"]):
            by_artist.setdefault(a["artist"], []).append(a)
    plans, notes = [], []
    downloads = config.settings.lidarr_downloads_path.rstrip("/") + "/"
    for artist, albums in sorted(by_artist.items()):
        found = await resolve.resolve(artist)
        if not found:
            notes.append(f"{artist}: not in the catalogue, left as it is")
            continue
        pref = await naming.preferred(found["musicBrainzArtistId"])
        if not pref:
            notes.append(f"{artist}: MusicBrainz has no {config.settings.name_locale} name, left as it is")
            continue
        downloaded: set[str] = set()
        try:
            lid = await lidarr.find_library_artist(found["musicBrainzArtistId"])
            if lid:
                downloaded = {f for dropped, imported in await lidarr.imported_paths(lid["id"])
                              if dropped.startswith(downloads) and (f := _folder_of(imported))}
        except httpx.HTTPError as e:
            notes.append(f"{artist}: Lidarr didn't answer ({e}); nothing retagged")
            continue
        p = Plan(artist, pref["name"], pref["sort"], found["musicBrainzArtistId"])
        for a in albums:
            (p.retag if a.get("folder") in downloaded else p.keep).append(a)
        plans.append(p)
    return plans, notes


def apply(plans: list[Plan]) -> int:
    music = cache.music_dir()
    changed = 0
    for p in plans:
        for a in p.retag:
            folder = os.path.realpath(os.path.join(music, a["folder"]))
            if folder.startswith(music + os.sep):
                changed += tags.rename_artist(tags.audio_files(folder), {p.artist}, p.name, p.sort)
        if p.retag:
            identity.set_library_name(p.mbid, p.name, original_name=p.artist, sort_name=p.sort)
    return changed


def describe(plans: list[Plan], notes: list[str]) -> str:
    lines = []
    for p in plans:
        lines.append(f"{p.artist}  ->  {p.name}   (sorted as {p.sort})")
        lines += [f"    retag   {a['name']}" for a in p.retag] or ["    retag   (nothing Lidarr downloaded)"]
        lines += [f"    leave   {a['name']}   (your own copy)" for a in p.keep]
    lines += [f"note: {n}" for n in notes]
    return "\n".join(lines) or "No artists with non-Latin names in the library."
