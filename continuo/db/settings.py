"""Runtime-tunable key/value settings (JSON values)."""
from .conn import connect, dumps, loads


def get(key: str, default=None):
    with connect() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return loads(r[0], default) if r else default


def put(key: str, value) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )


def all_settings() -> dict:
    with connect() as c:
        return {r[0]: loads(r[1]) for r in c.execute("SELECT key, value FROM settings")}
