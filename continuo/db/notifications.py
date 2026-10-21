"""The per-user notification inbox."""
from .conn import connect, dumps, loads, now, row, rows


def _decode(r: dict | None) -> dict | None:
    if r is not None:
        r["action"] = loads(r.get("action"))
    return r


def create(*, user_id: int, type: str, title: str, body: str | None,
           action: dict | None = None, request_id: int | None = None,
           delivered_via: list[str] | None = None) -> dict:
    """delivered_via: channels that already delivered it, recorded in the same INSERT so the row
    is never visible without them."""
    with connect() as c:
        cur = c.execute(
            "INSERT INTO notifications (user_id, type, title, body, action, request_id, created_at,"
            " delivered_via) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, type, title, body, dumps(action), request_id, now(),
             ",".join(delivered_via) if delivered_via else None),
        )
        return _decode(row(c.execute(
            "SELECT * FROM notifications WHERE id=?", (cur.lastrowid,)
        ).fetchone()))


def delivered_via(n: dict) -> list[str]:
    """The stored comma list as a list, e.g. ["homeassistant", "sse"]."""
    return [v for v in (n.get("delivered_via") or "").split(",") if v]


def mark_delivered(notification_id: int, via: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE notifications SET delivered_via = CASE WHEN delivered_via IS NULL THEN ? "
            "ELSE delivered_via || ',' || ? END WHERE id=?",
            (via, via, notification_id),
        )


def list_for_user(user_id: int, *, since_id: int = 0, unread_only: bool = False,
                  limit: int = 100, newest_first: bool = False) -> list[dict]:
    """Items with id > since_id: the oldest `limit` of them, or with newest_first the newest
    `limit`, newest first."""
    q = "SELECT * FROM notifications WHERE user_id=? AND id>?"
    if unread_only:
        q += " AND read_at IS NULL"
    q += " ORDER BY id DESC LIMIT ?" if newest_first else " ORDER BY id LIMIT ?"
    with connect() as c:
        return [_decode(r) for r in rows(c.execute(q, (user_id, since_id, limit)))]


def unread_count(user_id: int) -> int:
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user_id,)
        ).fetchone()[0]


def max_id(user_id: int) -> int:
    with connect() as c:
        return c.execute(
            "SELECT COALESCE(MAX(id), 0) FROM notifications WHERE user_id=?", (user_id,)
        ).fetchone()[0]


def mark_read(user_id: int, ids: list[int] | None = None) -> int:
    ts = now()
    with connect() as c:
        if ids is None:
            return c.execute(
                "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                (ts, user_id),
            ).rowcount
        if not ids:
            return 0
        return c.execute(
            f"UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL "
            f"AND id IN ({','.join('?' * len(ids))})",
            (ts, user_id, *ids),
        ).rowcount
