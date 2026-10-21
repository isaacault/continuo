"""SQLite connection helper. One short-lived connection per unit of work (as in lidseeker): the
app is single-process, the data is tiny, and WAL lets the loops and handlers interleave."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import config


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def seconds_since(value: str | None) -> float | None:
    t = parse_ts(value)
    if t is None:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


@contextmanager
def connect():
    path = config.settings.db_path
    new_file = not os.path.exists(path)
    # timeout = busy timeout: wait for a lock instead of failing under concurrent writers.
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if new_file:
        # The DB holds façade secrets in plaintext; keep it owner-only.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def dumps(value) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def loads(value: str | None, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def ping() -> bool:
    try:
        with connect() as c:
            c.execute("SELECT 1")
        return True
    except sqlite3.Error:
        return False
