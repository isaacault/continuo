"""Carry an account's Navidrome annotations from the store library to its own (#27).

A new library is new song and album ids, so the account's ratings, stars and playlists, which
Navidrome keeps by id, would stay behind on the store library it's about to lose access to.
Run after the account's library is indexed and before access changes: songs are matched by
path (library-relative, the same in every library), albums by folder. Play counts are not
carried: the only way to set one is a scrobble, which would also reach ListenBrainz."""
import logging

from .. import config
from ..db import library as lib_db
from ..upstream import navidrome

log = logging.getLogger("continuo")


async def _all(path: str, as_user: str, page: int = 500) -> list[dict]:
    out, start = [], 0
    while True:
        data, total = await navidrome.native_get(path, {"_start": start, "_end": start + page}, as_user=as_user)
        out += data or []
        start += len(data or [])
        if not data or len(data) < page or (total is not None and start >= total):
            return out


async def carry(nd_user: str, own_library: int, dry_run: bool = False) -> dict:
    store = config.settings.store_library_id
    songs = await _all("song", nd_user)
    mine = {s["path"]: s for s in songs if s.get("libraryId") == own_library}
    song_map = {s["id"]: mine[s["path"]] for s in songs
                if s.get("libraryId") == store and s.get("path") in mine}
    # Albums by folder (continuo's index knows each library's album folders).
    own_albums = lib_db.folders_in_library(own_library)
    album_map = {a["navidrome_id"]: own_albums[a["folder"]] for a in lib_db.all_albums()
                 if a.get("folder") in own_albums}
    albums = {a["id"]: a for a in await _all("album", nd_user)}

    out = {"songs": len(song_map), "unmatched_songs": sum(1 for s in songs if s.get("libraryId") == store) - len(song_map),
           "ratings": 0, "stars": 0, "playlists": 0, "playlist_tracks": 0, "unmatched_tracks": 0}
    ratings: list[tuple[str, int]] = []
    stars: list[str] = []
    by_id = {s["id"]: s for s in songs}
    for old_id, new in song_map.items():
        old = by_id[old_id]
        if old.get("rating") and not new.get("rating"):
            ratings.append((new["id"], old["rating"]))
        if old.get("starred") and not new.get("starred"):
            stars.append(new["id"])
    for old_id, new_id in album_map.items():
        old, new = albums.get(old_id) or {}, albums.get(new_id) or {}
        if old.get("rating") and not new.get("rating"):
            ratings.append((new_id, old["rating"]))
        if old.get("starred") and not new.get("starred"):
            stars.append(new_id)
    out["ratings"], out["stars"] = len(ratings), len(stars)

    body = await navidrome.subsonic("getPlaylists", as_user=nd_user)
    playlists = [p for p in (body.get("playlists") or {}).get("playlist") or [] if p.get("owner") == nd_user]
    rewrites: list[tuple[dict, list[str]]] = []
    for p in playlists:
        entries = ((await navidrome.subsonic("getPlaylist", {"id": p["id"]}, as_user=nd_user))
                   .get("playlist") or {}).get("entry") or []
        ids = [e["id"] for e in entries]
        new_ids = [song_map[i]["id"] if i in song_map else i for i in ids]
        out["unmatched_tracks"] += sum(1 for i in ids if i not in song_map and by_id.get(i, {}).get("libraryId") != own_library)
        if new_ids != ids:
            rewrites.append((p, new_ids))
            out["playlist_tracks"] += sum(1 for a, b in zip(ids, new_ids, strict=False) if a != b)
    out["playlists"] = len(rewrites)
    if dry_run:
        return out

    for item_id, rating in ratings:
        await navidrome.subsonic("setRating", {"id": item_id, "rating": rating}, as_user=nd_user)
    for i in range(0, len(stars), 100):
        await navidrome.subsonic("star", {"id": stars[i:i + 100]}, as_user=nd_user)
    for p, new_ids in rewrites:
        # createPlaylist with playlistId replaces the playlist's songs, keeping its id and order.
        await navidrome.subsonic("createPlaylist", {"playlistId": p["id"], "songId": new_ids}, as_user=nd_user)
        log.info("carry: %s's playlist %r now uses its own library", nd_user, p.get("name"))
    return out
