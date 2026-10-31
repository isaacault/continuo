"""The video index: Jellyfin's movies and series, kept in video_library so search and title pages
can say what's in the library without asking Jellyfin each time."""
import logging

from ..db.conn import connect, now, rows
from .upstream import jellyfin

log = logging.getLogger("continuo")


async def refresh() -> dict:
    items = await jellyfin.library_items()
    ts = now()
    with connect() as c:
        c.executemany(
            "INSERT INTO video_library (jellyfin_id, kind, title, year, tmdb_id, tvdb_id, imdb_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (jellyfin_id) DO UPDATE SET kind=excluded.kind, "
            "title=excluded.title, year=excluded.year, tmdb_id=excluded.tmdb_id, tvdb_id=excluded.tvdb_id, "
            "imdb_id=excluded.imdb_id, updated_at=excluded.updated_at",
            [(i["jellyfin_id"], i["kind"], i["title"], i["year"], i["tmdb_id"], i["tvdb_id"], i["imdb_id"], ts)
             for i in items])
        present = {i["jellyfin_id"] for i in items}
        gone = [r[0] for r in c.execute("SELECT jellyfin_id FROM video_library") if r[0] not in present]
        c.executemany("DELETE FROM video_library WHERE jellyfin_id=?", [(g,) for g in gone])
    out = {"items": len(items), "removed": len(gone)}
    log.info("video index: %d items, %d removed", out["items"], out["removed"])
    return out


def by_tmdb(kind: str, tmdb_id: int) -> dict | None:
    with connect() as c:
        r = rows(c.execute("SELECT * FROM video_library WHERE kind=? AND tmdb_id=? LIMIT 1", (kind, tmdb_id)))
    return r[0] if r else None


def all_items() -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM video_library ORDER BY title"))
