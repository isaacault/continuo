"""library_removals: albums each account took out of its own library (#27)."""
from .conn import connect, now, row, rows

_COLS = ("user_id", "folder", "title", "artist", "mb_release_group_id", "navidrome_album_id", "cache_item_id")


def add(item: dict) -> dict:
    with connect() as c:
        cur = c.execute(f"INSERT INTO library_removals ({', '.join(_COLS)}, removed_at) VALUES "
                        f"({', '.join('?' * (len(_COLS) + 1))})", tuple(item.get(k) for k in _COLS) + (now(),))
        return row(c.execute("SELECT * FROM library_removals WHERE id=?", (cur.lastrowid,)).fetchone())


def get(item_id: int) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM library_removals WHERE id=?", (item_id,)).fetchone())


def for_user(user_id: int) -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM library_removals WHERE user_id=? ORDER BY removed_at DESC, id DESC",
                              (user_id,)))


def delete(item_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM library_removals WHERE id=?", (item_id,))


def delete_older_than(ts: str) -> int:
    with connect() as c:
        return c.execute("DELETE FROM library_removals WHERE removed_at < ?", (ts,)).rowcount
