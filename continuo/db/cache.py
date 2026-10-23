"""media_cache: what's in the media cache (issue #23) and when it was last used."""
from .conn import connect, now, row, rows

_COLS = ("kind", "origin", "title", "artist", "mb_release_group_id", "mb_release_id",
         "mb_recording_id", "navidrome_album_id", "library_path", "cache_path",
         "lidarr_album_id", "lidarr_was_monitored", "bytes", "removed_by")


def add(item: dict) -> dict:
    # An explicit NULL would override the columns' defaults: fill what wasn't given.
    item = {"lidarr_was_monitored": 0, "bytes": 0, **{k: v for k, v in item.items() if v is not None}}
    ts = now()
    with connect() as c:
        cur = c.execute(
            f"INSERT INTO media_cache ({', '.join(_COLS)}, created_at, last_used_at) "
            f"VALUES ({', '.join('?' * (len(_COLS) + 2))})",
            tuple(item.get(k) for k in _COLS) + (ts, ts),
        )
        return row(c.execute("SELECT * FROM media_cache WHERE id=?", (cur.lastrowid,)).fetchone())


def get(item_id: int) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM media_cache WHERE id=?", (item_id,)).fetchone())


def list_items(origin: str | None = None) -> list[dict]:
    """Newest first."""
    with connect() as c:
        if origin:
            return rows(c.execute("SELECT * FROM media_cache WHERE origin=? ORDER BY created_at DESC, id DESC", (origin,)))
        return rows(c.execute("SELECT * FROM media_cache ORDER BY created_at DESC, id DESC"))


def least_recently_used() -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM media_cache ORDER BY last_used_at ASC, id ASC"))


def touch(item_id: int) -> None:
    with connect() as c:
        c.execute("UPDATE media_cache SET last_used_at=? WHERE id=?", (now(), item_id))


def total_bytes() -> int:
    with connect() as c:
        return c.execute("SELECT COALESCE(SUM(bytes), 0) FROM media_cache").fetchone()[0]


def delete(item_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM media_cache WHERE id=?", (item_id,))
