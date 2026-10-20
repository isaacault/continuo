"""Devices: one per signed-in app install, carrying its refresh token hash and façade secret."""
from .conn import connect, now, row, rows


def create(*, device_id: str, user_id: int, name: str, platform: str | None,
           refresh_hash: str, refresh_expires_at: str, facade_secret: str) -> dict:
    ts = now()
    with connect() as c:
        c.execute(
            "INSERT INTO devices (id, user_id, name, platform, refresh_hash, refresh_expires_at, "
            "facade_secret, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (device_id, user_id, name, platform, refresh_hash, refresh_expires_at,
             facade_secret, ts, ts),
        )
        return row(c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())


def get(device_id: str) -> dict | None:
    with connect() as c:
        return row(c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())


def by_refresh_hash(refresh_hash: str) -> tuple[dict | None, bool]:
    """(device, reused). `reused` is True when the hash is a token this device already rotated
    away from — someone is replaying an old refresh token."""
    with connect() as c:
        d = row(c.execute("SELECT * FROM devices WHERE refresh_hash=?", (refresh_hash,)).fetchone())
        if d:
            return d, False
        d = row(c.execute(
            "SELECT * FROM devices WHERE prev_refresh_hash=?", (refresh_hash,)
        ).fetchone())
        return d, d is not None


def rotate_refresh(device_id: str, new_hash: str, expires_at: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE devices SET prev_refresh_hash=refresh_hash, refresh_hash=?, "
            "refresh_expires_at=?, last_seen_at=? WHERE id=?",
            (new_hash, expires_at, now(), device_id),
        )


def list_for_user(user_id: int, include_revoked: bool = False) -> list[dict]:
    q = "SELECT * FROM devices WHERE user_id=?"
    if not include_revoked:
        q += " AND revoked_at IS NULL"
    with connect() as c:
        return rows(c.execute(q + " ORDER BY created_at", (user_id,)))


def active_secrets_for_username(username: str) -> list[tuple[str, str]]:
    """(device_id, facade_secret) for every live device of the user — the façade check."""
    with connect() as c:
        return [
            (r["id"], r["facade_secret"])
            for r in c.execute(
                "SELECT d.id, d.facade_secret FROM devices d JOIN users u ON u.id = d.user_id "
                "WHERE u.username=? AND d.revoked_at IS NULL",
                (username,),
            )
        ]


def revoke(device_id: str) -> bool:
    with connect() as c:
        return c.execute(
            "UPDATE devices SET revoked_at=?, refresh_hash=NULL, prev_refresh_hash=NULL "
            "WHERE id=? AND revoked_at IS NULL",
            (now(), device_id),
        ).rowcount > 0


def revoke_all_for_user(user_id: int) -> int:
    with connect() as c:
        return c.execute(
            "UPDATE devices SET revoked_at=?, refresh_hash=NULL, prev_refresh_hash=NULL "
            "WHERE user_id=? AND revoked_at IS NULL",
            (now(), user_id),
        ).rowcount


def touch(device_id: str) -> None:
    with connect() as c:
        c.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now(), device_id))


def set_push(device_id: str, kind: str | None, endpoint: str | None) -> None:
    with connect() as c:
        c.execute("UPDATE devices SET push_kind=?, push_endpoint=? WHERE id=?",
                  (kind, endpoint, device_id))
