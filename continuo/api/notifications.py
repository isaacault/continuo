"""Inbox and the SSE stream (PLAN.md §3.4)."""
import asyncio

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import events
from ..auth.deps import CurrentUser, require_user, user_from_token
from ..db import notifications as notif_db
from ..errors import ApiError
from ..notify.dispatch import notification_out
from .schemas import ReadIn

router = APIRouter(prefix="/v1", tags=["notifications"])
_bearer = HTTPBearer(auto_error=False)

KEEPALIVE_SECONDS = 25.0


@router.get("/notifications")
async def list_notifications(since: int = Query(0, ge=0), unread: bool = Query(False),
                             limit: int = Query(100, ge=1, le=500),
                             user: CurrentUser = Depends(require_user)) -> dict:
    items = notif_db.list_for_user(user.id, since_id=since, unread_only=unread, limit=limit)
    cursor = items[-1]["id"] if items else since
    return {"items": [notification_out(n) for n in items], "cursor": cursor,
            "unreadCount": notif_db.unread_count(user.id)}


@router.post("/notifications/read")
async def mark_read(body: ReadIn, user: CurrentUser = Depends(require_user)) -> dict:
    if not body.all and body.ids is None:
        raise ApiError(400, "bad_request", "Pass ids or all=true.")
    n = notif_db.mark_read(user.id, None if body.all else body.ids)
    return {"ok": True, "marked": n, "unreadCount": notif_db.unread_count(user.id)}


@router.get("/events")
async def sse(request: Request, token: str | None = Query(None),
              creds: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """Server-Sent Events: request.updated, request.available, notification.new.
    Auth: the Bearer header (preferred), or ?token= for EventSource clients that can't set
    headers (Caddy's access log redacts it)."""
    raw = creds.credentials if creds else token
    if not raw:
        raise ApiError(401, "missing_token", "Missing token.")
    user = user_from_token(raw)
    sub = events.subscribe(user.id, user.is_admin)

    async def gen():
        try:
            yield events.format_event("hello", {"user": user.username,
                                                "notificationCursor": notif_db.max_id(user.id)})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            events.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
