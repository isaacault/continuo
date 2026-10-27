"""lyrics_cache: LRCLIB's answers on disk (see the migration)."""
import json
from typing import Optional

from .conn import connect, now, parse_ts

_NONE_KEPT_S = 7 * 24 * 3600


def get(key: str) -> tuple[bool, Optional[dict]]:
    """(known, answer): known False when it must be asked; answer None when LRCLIB had none."""
    with connect() as c:
        r = c.execute("SELECT body, fetched_at FROM lyrics_cache WHERE key=?", (key,)).fetchone()
    if r is None:
        return False, None
    if r[0] is None:
        fetched = parse_ts(r[1])
        from datetime import datetime, timezone
        if fetched is None or (datetime.now(timezone.utc) - fetched).total_seconds() > _NONE_KEPT_S:
            return False, None
        return True, None
    return True, json.loads(r[0])


def put(key: str, body: Optional[dict]) -> None:
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else None
    with connect() as c:
        c.execute("INSERT INTO lyrics_cache (key, body, fetched_at) VALUES (?, ?, ?) "
                  "ON CONFLICT (key) DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at",
                  (key, text, now()))
