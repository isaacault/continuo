"""musicbrainz_cache: MusicBrainz answers on disk (see the migration)."""
import json

from .conn import connect, now


def get(key: str) -> tuple[dict, str] | None:
    """(answer, fetched_at), marking it used; None when not cached."""
    with connect() as c:
        r = c.execute("SELECT body, fetched_at FROM musicbrainz_cache WHERE key=?", (key,)).fetchone()
        if r is None:
            return None
        c.execute("UPDATE musicbrainz_cache SET used_at=? WHERE key=?", (now(), key))
    return json.loads(r[0]), r[1]


def put(key: str, body: dict) -> None:
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    ts = now()
    with connect() as c:
        c.execute("INSERT INTO musicbrainz_cache (key, body, bytes, fetched_at, used_at) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT (key) DO UPDATE SET body=excluded.body, bytes=excluded.bytes, "
                  "fetched_at=excluded.fetched_at, used_at=excluded.used_at",
                  (key, text, len(text.encode()), ts, ts))


def total_bytes() -> int:
    with connect() as c:
        return c.execute("SELECT COALESCE(SUM(bytes), 0) FROM musicbrainz_cache").fetchone()[0]


def prune(max_bytes: int) -> int:
    """Drop least recently used answers until the cache fits [max_bytes]. Returns how many."""
    dropped = 0
    with connect() as c:
        total = c.execute("SELECT COALESCE(SUM(bytes), 0) FROM musicbrainz_cache").fetchone()[0]
        if total <= max_bytes:
            return 0
        for key, size in c.execute("SELECT key, bytes FROM musicbrainz_cache ORDER BY used_at, key").fetchall():
            if total <= max_bytes * 0.9:          # a little headroom, so it isn't pruned on every write
                break
            c.execute("DELETE FROM musicbrainz_cache WHERE key=?", (key,))
            total -= size
            dropped += 1
    return dropped
