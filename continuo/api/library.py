"""Adding albums to and removing them from the library, and restoring them (#23, #27).

An account with its own library (#27) adds and removes in that library alone: its albums, its
Recently removed. Without one, removal is from the shared store, admins only by default
(LIBRARY_REMOVAL_ROLE=user opens it to every account)."""
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from .. import config
from ..auth.deps import CurrentUser, require_user
from ..db import users as users_db
from ..errors import ApiError
from ..library import ownership, removal

router = APIRouter(prefix="/v1/library", tags=["library"])


def may_remove(user: CurrentUser) -> bool:
    return ownership.has_own_library(user.id) or config.settings.library_removal_role != "admin" or user.is_admin


def require_remover(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if not may_remove(user):
        raise ApiError(403, "forbidden", "Only an admin can remove albums on this server.")
    return user


@router.get("/artist-names")
async def artist_names(_user: CurrentUser = Depends(require_user)) -> list[dict]:
    """Artists the library knows by another name than their original (Ichiko Aoba / 青葉市子):
    for showing both and searching either."""
    from ..db import identity
    return [{"name": r["library_name"], "originalName": r["original_name"], "sortName": r["sort_name"],
             "musicBrainzArtistId": r["mb_artist_id"]}
            for r in identity.artist_names() if r["original_name"]]


@router.get("/albums/{navidrome_id}/tracklist")
async def album_tracklist(request: Request, navidrome_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    """The whole album a library album is of, from the catalogue: for a single song filed under
    _singles, or an album the library only partly has, the app shows the rest beside what's
    there (Play now, Request). 404 when the album has no known release group."""
    from ..db import library as lib_db
    from ..ratelimit import catalog_limiter
    from ..search import merge
    from ..upstream import musicbrainz
    catalog_limiter.check(request)
    # The account's own copy (#27) is found through the store album it's a copy of.
    album = merge._LibraryIndex(lib_db.all_albums(), user.id).by_id.get(navidrome_id)
    rg = (album or {}).get("mbz_release_group_id")
    if not rg:
        raise ApiError(404, "not_found", "No catalogue album is known for that library album.")
    return {"musicBrainzReleaseGroupId": rg, "tracks": await musicbrainz.album_tracks(rg)}


@router.get("/singles")
async def singles(user: CurrentUser = Depends(require_user)) -> dict:
    """The account's library albums that hold single songs rather than the album (songs requested
    or kept one at a time, filed under _singles): {albums: [{navidromeAlbumId, have, expected}]}.
    The app shows them under Songs, and under Albums and Artists only when asked."""
    from ..db import library as lib_db
    from ..library.singles import Singles, is_singles
    from ..search import merge
    index = merge._LibraryIndex(lib_db.all_albums(), user.id)
    counts = Singles()
    out = []
    for album in index.by_id.values():
        if not is_singles(album.get("folder")):
            continue
        mine = index.mine(album)
        if mine is None or any(o["navidromeAlbumId"] == mine for o in out):
            continue
        have = int(album.get("song_count") or 1)
        out.append({"navidromeAlbumId": mine, "have": have,
                    "expected": max(counts.expected.get(album.get("folder") or "", 0), have)})
    return {"albums": out}


@router.get("/lyrics")
async def lyrics(request: Request, artist: str = Query(..., min_length=1, max_length=300),
                 title: str = Query(..., min_length=1, max_length=300), album: Optional[str] = Query(None, max_length=300),
                 durationMs: Optional[int] = Query(None, ge=0), _user: CurrentUser = Depends(require_user)) -> dict:
    """A song's lyrics from LRCLIB, for one the library has none for or isn't in the library
    (Play now): {synced, lines: [{startMs, text}], instrumental, source}. 404 when none are known."""
    import httpx

    from ..ratelimit import catalog_limiter
    from ..upstream import lrclib
    catalog_limiter.check(request)
    try:
        found = await lrclib.lyrics(artist, title, album, round(durationMs / 1000) if durationMs else None)
    except httpx.HTTPError as e:
        raise ApiError(502, "lyrics_unavailable", "LRCLIB can't be reached right now.", True) from e
    if found is None:
        raise ApiError(404, "no_lyrics", "No lyrics are known for this song.")
    return found


@router.post("/albums/{navidrome_id}/remove")
async def remove_album(navidrome_id: str, user: CurrentUser = Depends(require_remover)) -> dict:
    """Out of the account's own library; or, without one, out of the store into the media cache.
    Restorable until evicted."""
    if ownership.has_own_library(user.id):
        return await ownership.remove(users_db.get_by_id(user.id), navidrome_id)
    return await removal.remove(navidrome_id, user.username)


@router.get("/removed")
async def list_removed(user: CurrentUser = Depends(require_remover)) -> list[dict]:
    """Removed albums still restorable, newest first: the account's own, or the store's."""
    if ownership.has_own_library(user.id):
        return ownership.removed(users_db.get_by_id(user.id))
    return removal.removed()


@router.post("/removed/{item_id}/restore")
async def restore_album(item_id: int, user: CurrentUser = Depends(require_remover)) -> dict:
    if ownership.has_own_library(user.id):
        return await ownership.restore(users_db.get_by_id(user.id), item_id)
    return await removal.restore(item_id)


@router.post("/albums/{store_album_id}/add")
async def add_album(store_album_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    """Put an album that's already on the server (state on_server) into the account's library:
    instant, nothing is downloaded."""
    return await ownership.add(users_db.get_by_id(user.id), store_album_id)
