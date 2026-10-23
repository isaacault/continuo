"""artist_names and album_matches: what continuo learned about library music without MusicBrainz
ids (see migrations/0004)."""
from .conn import connect, now, rows


def set_library_name(mb_artist_id: str, library_name: str, original_name: str | None = None,
                     sort_name: str | None = None) -> None:
    """Remember the library's name for a MusicBrainz artist; original and sort names are kept
    when not given again."""
    with connect() as c:
        c.execute("INSERT INTO artist_names (mb_artist_id, library_name, original_name, sort_name, updated_at) "
                  "VALUES (?, ?, ?, ?, ?) ON CONFLICT (mb_artist_id) DO UPDATE SET "
                  "library_name=excluded.library_name, "
                  "original_name=COALESCE(excluded.original_name, artist_names.original_name), "
                  "sort_name=COALESCE(excluded.sort_name, artist_names.sort_name), updated_at=excluded.updated_at",
                  (mb_artist_id, library_name, original_name, sort_name, now()))


def artist_names() -> list[dict]:
    """Every remembered artist: mb_artist_id, library_name, original_name, sort_name."""
    with connect() as c:
        return rows(c.execute("SELECT mb_artist_id, library_name, original_name, sort_name FROM artist_names "
                              "ORDER BY library_name"))


def library_name(mb_artist_id: str | None) -> str | None:
    if not mb_artist_id:
        return None
    with connect() as c:
        r = c.execute("SELECT library_name FROM artist_names WHERE mb_artist_id=?", (mb_artist_id,)).fetchone()
        return r[0] if r else None


def library_names() -> dict[str, str]:
    """mb_artist_id -> library name."""
    with connect() as c:
        return {r[0]: r[1] for r in c.execute("SELECT mb_artist_id, library_name FROM artist_names")}


def set_album_match(navidrome_id: str, mb_release_group_id: str | None) -> None:
    with connect() as c:
        c.execute("INSERT INTO album_matches (navidrome_id, mb_release_group_id, matched_at) VALUES (?, ?, ?) "
                  "ON CONFLICT (navidrome_id) DO UPDATE SET mb_release_group_id=excluded.mb_release_group_id, "
                  "matched_at=excluded.matched_at", (navidrome_id, mb_release_group_id, now()))


def album_matches() -> dict[str, dict]:
    """navidrome_id -> {mb_release_group_id, matched_at}, misses included."""
    with connect() as c:
        return {r["navidrome_id"]: r for r in rows(c.execute("SELECT * FROM album_matches"))}
