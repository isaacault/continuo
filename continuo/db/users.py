"""Users (ported from lidseeker's db.py, + navidrome_username)."""
from .conn import connect, now, row, rows


def get(username: str) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def get_by_id(user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    with connect() as c:
        return row(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def list_all() -> list[dict]:
    with connect() as c:
        return rows(c.execute(
            "SELECT id, username, role, navidrome_username, created_at FROM users ORDER BY id"
        ))


def count() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_admins() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]


def admin_ids() -> set[int]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT id FROM users WHERE role='admin'")}


def usernames_by_id() -> dict[int, str]:
    with connect() as c:
        return {r["id"]: r["username"] for r in c.execute("SELECT id, username FROM users")}


def create(username: str, password_hash: str, role: str = "user",
           navidrome_username: str | None = None, created_at: str | None = None) -> dict:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO users (username, password_hash, role, navidrome_username, created_at) "
            "VALUES (?,?,?,?,?)",
            (username, password_hash, role, navidrome_username, created_at or now()),
        )
        return row(c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())


def delete(user_id: int) -> bool:
    with connect() as c:
        return c.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0


def set_password(user_id: int, password_hash: str) -> None:
    with connect() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))


def set_role(user_id: int, role: str) -> None:
    with connect() as c:
        c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def set_navidrome_username(user_id: int, navidrome_username: str | None) -> None:
    with connect() as c:
        c.execute("UPDATE users SET navidrome_username=? WHERE id=?", (navidrome_username, user_id))


def bump_token_version(user_id: int) -> None:
    """Invalidate every outstanding access token for the user."""
    with connect() as c:
        c.execute("UPDATE users SET token_version = token_version + 1 WHERE id=?", (user_id,))


def navidrome_name(user: dict) -> str:
    return user.get("navidrome_username") or user["username"]
