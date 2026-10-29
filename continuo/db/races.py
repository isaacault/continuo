"""races: second sources racing a slow download (#31)."""
import json

from .conn import connect, now, row, rows

ACTIVE = ("downloading", "importing")


def _out(r: dict | None) -> dict | None:
    if r is not None:
        r["files"] = json.loads(r.get("files") or "[]")
    return r


def add(request_id: int, source: str, *, username: str | None = None, remote_folder: str | None = None,
        local_folder: str | None = None, files: list | None = None, state: str = "downloading",
        detail: str | None = None) -> dict:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO races (request_id, source, username, remote_folder, local_folder, files, state, detail, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, source, username, remote_folder, local_folder, json.dumps(files or []), state, detail, now()))
        return _out(row(c.execute("SELECT * FROM races WHERE id=?", (cur.lastrowid,)).fetchone()))


def for_request(request_id: int) -> list[dict]:
    with connect() as c:
        return [_out(r) for r in rows(c.execute("SELECT * FROM races WHERE request_id=? ORDER BY id", (request_id,)))]


def active() -> list[dict]:
    with connect() as c:
        return [_out(r) for r in rows(c.execute(
            f"SELECT * FROM races WHERE state IN ({','.join('?' * len(ACTIVE))}) ORDER BY id", ACTIVE))]


def finish(race_id: int, state: str, detail: str | None = None) -> None:
    with connect() as c:
        c.execute("UPDATE races SET state=?, detail=COALESCE(?, detail), finished_at=? WHERE id=?",
                  (state, detail, now(), race_id))


def set_folder(race_id: int, local_folder: str) -> None:
    with connect() as c:
        c.execute("UPDATE races SET local_folder=? WHERE id=?", (local_folder, race_id))


def set_state(race_id: int, state: str, detail: str | None = None) -> None:
    with connect() as c:
        c.execute("UPDATE races SET state=?, detail=COALESCE(?, detail) WHERE id=?", (state, detail, race_id))
