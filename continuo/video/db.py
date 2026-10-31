"""video_requests (the video part's request rows; states as music's)."""
import json

from ..db.conn import connect, now, row, rows

ACTIVE = ("requested", "searching", "downloading", "importing")


def create(*, kind: str, tmdb_id: int, title: str | None, year: int | None, poster_path: str | None,
           user_id: int | None, seasons: list[int] | None = None) -> dict:
    ts = now()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO video_requests (kind, tmdb_id, title, year, poster_path, seasons, status, user_id,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?)",
            (kind, tmdb_id, title, year, poster_path, json.dumps(seasons) if seasons is not None else None,
             user_id, ts, ts))
        return row(c.execute("SELECT * FROM video_requests WHERE id=?", (cur.lastrowid,)).fetchone())


def get(request_id: int) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM video_requests WHERE id=?", (request_id,)).fetchone())


def update(request_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    with connect() as c:
        c.execute(f"UPDATE video_requests SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                  (*fields.values(), request_id))


def delete(request_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM video_requests WHERE id=?", (request_id,))


def active_for(kind: str, tmdb_id: int) -> dict | None:
    with connect() as c:
        return row(c.execute(f"SELECT * FROM video_requests WHERE kind=? AND tmdb_id=? AND status IN "
                             f"({','.join('?' * len(ACTIVE))}) ORDER BY id DESC LIMIT 1",
                             (kind, tmdb_id, *ACTIVE)).fetchone())


def active() -> list[dict]:
    with connect() as c:
        return rows(c.execute(f"SELECT * FROM video_requests WHERE status IN ({','.join('?' * len(ACTIVE))})",
                              ACTIVE))


def for_user(user_id: int | None) -> list[dict]:
    """A user's requests, newest first; None: everyone's (admins)."""
    with connect() as c:
        if user_id is None:
            return rows(c.execute("SELECT * FROM video_requests ORDER BY id DESC"))
        return rows(c.execute("SELECT * FROM video_requests WHERE user_id=? ORDER BY id DESC", (user_id,)))
