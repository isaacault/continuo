"""Request notifications: inbox row + SSE for the app, Home Assistant push for the phone.

Policy: ntfy is for outages only (Uptime Kuma owns that), so nothing here talks to ntfy.
What notifies: available, needs_attention, error on add. Never a per-search-cycle message."""
import logging

from .. import events
from ..db import notifications as notif_db
from ..db import requests as req_db
from ..db import users as users_db
from ..db.conn import now
from ..requests.view import deep_link
from ..upstream import homeassistant

log = logging.getLogger("continuo")

_TEMPLATES = {
    "request_available": ("Ready to play", "{title} — {artist}"),
    "request_needs_attention": ("A request needs a look", "{title} — {artist}: {detail}"),
    "request_error": ("Couldn't request an album", "{title} — {artist}: {detail}"),
}


def _action(row: dict, type: str) -> dict:
    if type == "request_available" and row.get("navidrome_album_id"):
        return {"type": "open_album", "navidromeAlbumId": row["navidrome_album_id"],
                "requestId": row["id"], "deepLink": deep_link(row["navidrome_album_id"])}
    return {"type": "open_request", "requestId": row["id"],
            "deepLink": f"polyphony://requests/{row['id']}"}


def notification_out(n: dict) -> dict:
    return {
        "id": n["id"], "type": n["type"], "title": n["title"], "body": n["body"],
        "createdAt": n["created_at"], "readAt": n["read_at"], "action": n["action"],
        "requestId": n["request_id"],
        "deliveredVia": notif_db.delivered_via(n),
    }


async def to_user(uid: int, *, type: str, title: str, body: str, action: dict, tag: str,
                  request_id: int | None = None) -> dict:
    """Push (Home Assistant) and store one inbox notification, then send it over SSE: what every
    part's events go through (music's requests below, video's in continuo.video)."""
    user = users_db.get_by_id(uid)
    pushed = await homeassistant.notify(title=title, message=body, tag=tag, click_action=action.get("deepLink"),
                                        kind=type, username=user["username"] if user else None)
    n = notif_db.create(user_id=uid, type=type, title=title, body=body, action=action, request_id=request_id,
                        delivered_via=["homeassistant"] if pushed else [])
    if events.publish("notification.new", notification_out(n), uid):
        notif_db.mark_delivered(n["id"], "sse")
    return n


async def request_event(row: dict, type: str, detail: str = "") -> dict | None:
    """Notify the request's owner (or every admin for an orphaned request)."""
    title_t, body_t = _TEMPLATES[type]
    body = body_t.format(title=row.get("title") or "Album", artist=row.get("artist") or "",
                         detail=detail).strip(" —:")
    action = _action(row, type)
    recipients = [row["user_id"]] if row.get("user_id") else sorted(users_db.admin_ids())
    first = None
    for uid in recipients:
        # Push first, then create the inbox row with the outcome already on it. A poller (or the
        # SSE payload) therefore never sees this item without its final "homeassistant" status,
        # and the row's id is assigned when it becomes visible, so `since` cursors stay correct.
        user = users_db.get_by_id(uid)
        pushed = await homeassistant.notify(
            title=title_t, message=body, tag=f"request-{row['id']}",
            click_action=action.get("deepLink"), kind=type,
            username=user["username"] if user else None,
        )
        n = notif_db.create(user_id=uid, type=type, title=title_t, body=body, action=action,
                            request_id=row["id"],
                            delivered_via=["homeassistant"] if pushed else [])
        first = first or n
        if events.publish("notification.new", notification_out(n), uid):
            notif_db.mark_delivered(n["id"], "sse")
    if type == "request_available":
        req_db.update(row["id"], notified_at=now())
    req_db.add_event(row["id"], "notified", {"type": type, "recipients": recipients})
    return first
