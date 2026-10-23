"""account_libraries: each account's own Navidrome library (#27)."""
from .conn import connect, now, row, rows


def get(user_id: int) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM account_libraries WHERE user_id=?", (user_id,)).fetchone())


def set(user_id: int, library_id: int, navidrome_user_id: str | None) -> None:
    with connect() as c:
        c.execute("INSERT INTO account_libraries (user_id, navidrome_library_id, navidrome_user_id, created_at) "
                  "VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET navidrome_library_id=excluded.navidrome_library_id, "
                  "navidrome_user_id=excluded.navidrome_user_id", (user_id, library_id, navidrome_user_id, now()))


def all() -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM account_libraries"))
