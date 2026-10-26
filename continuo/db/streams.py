"""streams: Play now fetches (see the migration)."""
from .conn import connect, dumps, loads, now, row

_UPDATABLE = {"status", "username", "filename", "size", "extension", "tried", "cache_item_id", "error",
              "title", "artist", "duration_ms"}


def _decode(r: dict | None) -> dict | None:
    if r is not None:
        r["tried"] = loads(r.get("tried"), {})
    return r


def create(stream_id: str, user_id: int | None, recording_id: str, album_rg: str | None,
           title: str | None, artist: str | None, duration_ms: int | None) -> dict:
    ts = now()
    with connect() as c:
        c.execute("INSERT INTO streams (id, user_id, recording_id, album_rg, title, artist, duration_ms, status, "
                  "tried, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'searching', '{}', ?, ?)",
                  (stream_id, user_id, recording_id, album_rg, title, artist, duration_ms, ts, ts))
    return get(stream_id)


def get(stream_id: str) -> dict | None:
    with connect() as c:
        return _decode(row(c.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()))


def ready_for(recording_id: str) -> dict | None:
    """The newest finished stream of this recording still in the cache, for an instant replay."""
    with connect() as c:
        return _decode(row(c.execute(
            "SELECT s.* FROM streams s JOIN media_cache m ON m.id = s.cache_item_id "
            "WHERE s.recording_id=? AND s.status='ready' ORDER BY s.updated_at DESC LIMIT 1",
            (recording_id,)).fetchone()))


def update(stream_id: str, **fields) -> dict | None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown stream fields: {sorted(unknown)}")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        args = [dumps(v) if k == "tried" else v for k, v in fields.items()]
        with connect() as c:
            c.execute(f"UPDATE streams SET {sets}, updated_at=? WHERE id=?", (*args, now(), stream_id))
    return get(stream_id)
