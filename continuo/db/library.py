"""library_albums: continuo's disposable index of Navidrome's albums, for matching."""
from .conn import connect, now, row, rows

_COLS = ("navidrome_id", "name", "artist", "year", "song_count", "mbz_album_id",
         "mbz_release_group_id", "folder", "norm_key", "source_updated_at")


def upsert_many(albums: list[dict]) -> None:
    ts = now()
    with connect() as c:
        c.executemany(
            f"INSERT INTO library_albums ({', '.join(_COLS)}, updated_at) "
            f"VALUES ({', '.join('?' * (len(_COLS) + 1))}) "
            "ON CONFLICT (navidrome_id) DO UPDATE SET "
            + ", ".join(f"{k}=excluded.{k}" for k in _COLS[1:]) + ", updated_at=excluded.updated_at",
            [tuple(a.get(k) for k in _COLS) + (ts,) for a in albums],
        )


def delete_missing(present_ids: set[str]) -> int:
    with connect() as c:
        existing = {r[0] for r in c.execute("SELECT navidrome_id FROM library_albums")}
        gone = existing - present_ids
        c.executemany("DELETE FROM library_albums WHERE navidrome_id=?", [(g,) for g in gone])
        return len(gone)


def all_albums() -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM library_albums"))


def source_versions() -> dict[str, tuple[str | None, str | None]]:
    """navidrome_id -> (source_updated_at, folder): lets a refresh skip unchanged albums."""
    with connect() as c:
        return {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT navidrome_id, source_updated_at, folder FROM library_albums"
        )}


def get(navidrome_id: str) -> dict | None:
    with connect() as c:
        return row(c.execute(
            "SELECT * FROM library_albums WHERE navidrome_id=?", (navidrome_id,)
        ).fetchone())


def delete(navidrome_id: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM library_albums WHERE navidrome_id=?", (navidrome_id,))


def by_folder(folder: str) -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM library_albums WHERE folder=?", (folder,)))


def by_release_group(mbid: str) -> list[dict]:
    with connect() as c:
        return rows(c.execute(
            "SELECT * FROM library_albums WHERE mbz_release_group_id=?", (mbid,)
        ))


def by_release_ids(release_ids: list[str]) -> list[dict]:
    if not release_ids:
        return []
    with connect() as c:
        return rows(c.execute(
            f"SELECT * FROM library_albums WHERE mbz_album_id IN ({','.join('?' * len(release_ids))})",
            release_ids,
        ))


def count() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM library_albums").fetchone()[0]
