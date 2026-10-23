"""library_members: which album folders each account's library has (#27)."""
from .conn import connect, now


def add(user_id: int, folder: str, origin: str = "add") -> bool:
    """True when it wasn't there yet."""
    with connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO library_members (user_id, folder, origin, added_at) VALUES (?, ?, ?, ?)",
                        (user_id, folder, origin, now()))
        return cur.rowcount > 0


def add_many(user_id: int, folders: list[str], origin: str) -> int:
    ts = now()
    with connect() as c:
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO library_members (user_id, folder, origin, added_at) VALUES (?, ?, ?, ?)",
                      [(user_id, f, origin, ts) for f in folders])
        return c.total_changes - before


def remove(user_id: int, folder: str) -> bool:
    with connect() as c:
        return c.execute("DELETE FROM library_members WHERE user_id=? AND folder=?", (user_id, folder)).rowcount > 0


def has(user_id: int, folder: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM library_members WHERE user_id=? AND folder=?", (user_id, folder)).fetchone() is not None


def folders(user_id: int) -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT folder FROM library_members WHERE user_id=?", (user_id,))}


def holders(folder: str) -> set[int]:
    """The accounts whose library has this album folder."""
    with connect() as c:
        return {r[0] for r in c.execute("SELECT user_id FROM library_members WHERE folder=?", (folder,))}


def user_ids() -> set[int]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT user_id FROM library_members")}
