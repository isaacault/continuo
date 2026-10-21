"""Requests and their append-only event log."""
from .conn import connect, dumps, loads, now, row, rows

# Statuses a request can be in. No terminal "failed" — see the migration.
ACTIVE = ("pending", "searching", "downloading", "importing", "scanning", "needs_attention")
ALL_STATUSES = ACTIVE + ("available", "error")

_UPDATABLE = {
    "title", "artist", "year", "artwork_url", "note", "lidarr_artist_id", "lidarr_album_id",
    "lidarr_release_id", "navidrome_album_id", "status", "still_looking", "search_cycles",
    "last_cycle_at", "last_search_at", "last_source", "import_paths", "imported_at",
    "scan_started_at", "attention", "error", "user_id", "available_at", "notified_at",
    "download_started_at", "first_file_at", "first_source",
}
_JSON = {"import_paths", "attention"}


def _decode(r: dict | None) -> dict | None:
    if r is None:
        return None
    r["import_paths"] = loads(r.get("import_paths"), [])
    r["attention"] = loads(r.get("attention"))
    r["still_looking"] = bool(r.get("still_looking"))
    return r


def create(*, mb_release_group_id: str, title: str | None, artist: str | None,
           year: int | None, artwork_url: str | None, user_id: int | None,
           note: str | None = None, kind: str = "album", status: str = "pending",
           created_at: str | None = None) -> dict:
    """Insert a request, or restart an existing row for the same album (an errored or
    no-longer-present one being requested again)."""
    ts = now()
    with connect() as c:
        c.execute(
            """
            INSERT INTO requests (kind, mb_release_group_id, title, artist, year, artwork_url,
                                  note, status, user_id, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (kind, mb_release_group_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist, year=excluded.year,
                artwork_url=excluded.artwork_url, note=COALESCE(excluded.note, requests.note),
                status=excluded.status, user_id=COALESCE(excluded.user_id, requests.user_id),
                error=NULL, attention=NULL, still_looking=0, search_cycles=0,
                last_cycle_at=NULL, navidrome_album_id=NULL, available_at=NULL,
                notified_at=NULL, imported_at=NULL, scan_started_at=NULL, import_paths=NULL,
                updated_at=excluded.updated_at
            """,
            (kind, mb_release_group_id, title, artist, year, artwork_url, note, status,
             user_id, created_at or ts, ts),
        )
        return _decode(row(c.execute(
            "SELECT * FROM requests WHERE kind=? AND mb_release_group_id=?",
            (kind, mb_release_group_id),
        ).fetchone()))


def get(request_id: int) -> dict | None:
    with connect() as c:
        return _decode(row(c.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()))


def by_release_group(mbid: str, kind: str = "album") -> dict | None:
    with connect() as c:
        return _decode(row(c.execute(
            "SELECT * FROM requests WHERE kind=? AND mb_release_group_id=?", (kind, mbid)
        ).fetchone()))


def by_lidarr_album(lidarr_album_id: int) -> dict | None:
    with connect() as c:
        return _decode(row(c.execute(
            "SELECT * FROM requests WHERE lidarr_album_id=? ORDER BY id DESC LIMIT 1",
            (lidarr_album_id,),
        ).fetchone()))


def list_requests(*, user_id: int | None = None, statuses: tuple[str, ...] | None = None,
                  limit: int | None = None, before_id: int | None = None) -> list[dict]:
    q, args = "SELECT * FROM requests WHERE 1=1", []
    if user_id is not None:
        q += " AND user_id=?"
        args.append(user_id)
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    if before_id is not None:
        q += " AND id<?"
        args.append(before_id)
    q += " ORDER BY id DESC"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    with connect() as c:
        return [_decode(r) for r in rows(c.execute(q, args))]


def active() -> list[dict]:
    return list_requests(statuses=ACTIVE)


def active_by_release_group() -> dict[str, dict]:
    """Live requests keyed by release-group MBID — for search availability and the request lock."""
    return {r["mb_release_group_id"]: r for r in list_requests(statuses=ACTIVE)}


def pending_unprocessed() -> list[dict]:
    """Rows still 'pending' with nothing in Lidarr: their add task died with a restart."""
    with connect() as c:
        return [_decode(r) for r in rows(c.execute(
            "SELECT * FROM requests WHERE status='pending' AND lidarr_album_id IS NULL"
        ))]


def update(request_id: int, **fields) -> dict | None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown request fields: {sorted(unknown)}")
    if not fields:
        return get(request_id)
    sets, args = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        if k in _JSON:
            v = dumps(v)
        elif k == "still_looking":
            v = int(bool(v))
        args.append(v)
    sets.append("updated_at=?")
    args.extend([now(), request_id])
    with connect() as c:
        c.execute(f"UPDATE requests SET {', '.join(sets)} WHERE id=?", args)
    return get(request_id)


def delete(request_id: int) -> dict | None:
    with connect() as c:
        r = row(c.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())
        if r:
            c.execute("DELETE FROM requests WHERE id=?", (request_id,))
        return _decode(r)


def delete_available(user_id: int | None = None) -> int:
    with connect() as c:
        if user_id is None:
            return c.execute("DELETE FROM requests WHERE status='available'").rowcount
        return c.execute(
            "DELETE FROM requests WHERE status='available' AND user_id=?", (user_id,)
        ).rowcount


def latency_rows(since: str) -> list[dict]:
    """Requests created since `since` with their latency milestones (issue #10)."""
    with connect() as c:
        return rows(c.execute(
            "SELECT id, title, artist, status, created_at, download_started_at, first_file_at, "
            "available_at, first_source, last_source FROM requests WHERE created_at >= ? "
            "ORDER BY id DESC", (since,),
        ))


def stats() -> dict:
    with connect() as c:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) AS n FROM requests GROUP BY status"
        )}
        still = c.execute(
            "SELECT COUNT(*) FROM requests WHERE still_looking=1 AND status NOT IN ('available','error')"
        ).fetchone()[0]
    return {
        "total": sum(counts.values()),
        "byStatus": {s: counts.get(s, 0) for s in ALL_STATUSES},
        "stillLooking": still,
    }


# --- events -------------------------------------------------------------------------------
def add_event(request_id: int, type: str, detail: dict | None = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO request_events (request_id, at, type, detail) VALUES (?,?,?,?)",
            (request_id, now(), type, dumps(detail)),
        )


def events(request_id: int, limit: int = 500) -> list[dict]:
    with connect() as c:
        out = rows(c.execute(
            "SELECT at, type, detail FROM request_events WHERE request_id=? ORDER BY id LIMIT ?",
            (request_id, limit),
        ))
    for e in out:
        e["detail"] = loads(e["detail"])
    return out
