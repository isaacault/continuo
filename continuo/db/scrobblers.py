"""scrobblers: where each account's Play now songs are scrobbled (see the migration)."""
from .conn import connect, now


def all_for(user_id: int) -> dict[str, dict]:
    with connect() as c:
        rows = c.execute("SELECT service, secret, account FROM scrobblers WHERE user_id=?", (user_id,)).fetchall()
    return {r[0]: {"secret": r[1], "account": r[2]} for r in rows}


def put(user_id: int, service: str, secret: str, account: str | None) -> None:
    with connect() as c:
        c.execute("INSERT INTO scrobblers (user_id, service, secret, account, created_at) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT (user_id, service) DO UPDATE SET secret=excluded.secret, account=excluded.account",
                  (user_id, service, secret, account, now()))


def delete(user_id: int, service: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM scrobblers WHERE user_id=? AND service=?", (user_id, service))
