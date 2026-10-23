"""Adding albums to and removing them from the library, and restoring them (#23, #27).

An account with its own library (#27) adds and removes in that library alone: its albums, its
Recently removed. Without one, removal is from the shared store, admins only by default
(LIBRARY_REMOVAL_ROLE=user opens it to every account)."""
from fastapi import APIRouter, Depends

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
