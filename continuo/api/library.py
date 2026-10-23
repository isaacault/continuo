"""Removing albums from the library and restoring them (issue #23). Admins only: it deletes
music for everyone on the server."""
from fastapi import APIRouter, Depends

from ..auth.deps import CurrentUser, require_admin
from ..library import removal

router = APIRouter(prefix="/v1/library", tags=["library"])


@router.post("/albums/{navidrome_id}/remove")
async def remove_album(navidrome_id: str, admin: CurrentUser = Depends(require_admin)) -> dict:
    """Move the album into the media cache: gone from the library, restorable until evicted."""
    return await removal.remove(navidrome_id, admin.username)


@router.get("/removed")
async def list_removed(_admin: CurrentUser = Depends(require_admin)) -> list[dict]:
    """Removed albums still in the media cache, newest first."""
    return removal.removed()


@router.post("/removed/{item_id}/restore")
async def restore_album(item_id: int, _admin: CurrentUser = Depends(require_admin)) -> dict:
    return await removal.restore(item_id)
