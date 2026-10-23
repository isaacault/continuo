"""Removing albums from the library and restoring them (issue #23). Admins by default while the
library is one shared set of files; LIBRARY_REMOVAL_ROLE=user opens it to every account. Per-user
libraries (#27) will make removal "from my library" for everyone."""
from fastapi import APIRouter, Depends

from .. import config
from ..auth.deps import CurrentUser, require_user
from ..errors import ApiError
from ..library import removal

router = APIRouter(prefix="/v1/library", tags=["library"])


def may_remove(user: CurrentUser) -> bool:
    return config.settings.library_removal_role != "admin" or user.is_admin


def require_remover(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if not may_remove(user):
        raise ApiError(403, "forbidden", "Only an admin can remove albums on this server.")
    return user


@router.post("/albums/{navidrome_id}/remove")
async def remove_album(navidrome_id: str, user: CurrentUser = Depends(require_remover)) -> dict:
    """Move the album into the media cache: gone from the library, restorable until evicted."""
    return await removal.remove(navidrome_id, user.username)


@router.get("/removed")
async def list_removed(_user: CurrentUser = Depends(require_remover)) -> list[dict]:
    """Removed albums still in the media cache, newest first."""
    return removal.removed()


@router.post("/removed/{item_id}/restore")
async def restore_album(item_id: int, _user: CurrentUser = Depends(require_remover)) -> dict:
    return await removal.restore(item_id)
