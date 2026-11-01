"""My List and ratings: each account's own, for any title (docs/design/video.md §7)."""
from ..db.conn import connect, now, rows


def entries(user_id: int) -> list[dict]:
    with connect() as c:
        return rows(c.execute("SELECT * FROM video_my_list WHERE user_id=? ORDER BY added_at DESC, rowid DESC",
                              (user_id,)))


def contains(user_id: int, kind: str, tmdb_id: int) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM video_my_list WHERE user_id=? AND kind=? AND tmdb_id=?",
                         (user_id, kind, tmdb_id)).fetchone() is not None


def add(user_id: int, kind: str, tmdb_id: int, *, title: str, year: int | None, poster_path: str | None,
        backdrop_path: str | None) -> None:
    with connect() as c:
        c.execute("INSERT INTO video_my_list (user_id, kind, tmdb_id, title, year, poster_path, backdrop_path, added_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                  (user_id, kind, tmdb_id, title, year, poster_path, backdrop_path, now()))


def remove(user_id: int, kind: str, tmdb_id: int) -> bool:
    with connect() as c:
        return c.execute("DELETE FROM video_my_list WHERE user_id=? AND kind=? AND tmdb_id=?",
                         (user_id, kind, tmdb_id)).rowcount > 0


def rating(user_id: int, kind: str, tmdb_id: int) -> int:
    """-1, 1 or 2; 0 when not rated."""
    with connect() as c:
        r = c.execute("SELECT rating FROM video_ratings WHERE user_id=? AND kind=? AND tmdb_id=?",
                      (user_id, kind, tmdb_id)).fetchone()
    return r[0] if r else 0


def rate(user_id: int, kind: str, tmdb_id: int, value: int, title: str) -> None:
    """value 0 clears the rating."""
    with connect() as c:
        if value == 0:
            c.execute("DELETE FROM video_ratings WHERE user_id=? AND kind=? AND tmdb_id=?", (user_id, kind, tmdb_id))
        else:
            c.execute("INSERT INTO video_ratings (user_id, kind, tmdb_id, rating, title, rated_at) VALUES (?, ?, ?, ?, ?, ?)"
                      " ON CONFLICT (user_id, kind, tmdb_id) DO UPDATE SET rating=excluded.rating, rated_at=excluded.rated_at",
                      (user_id, kind, tmdb_id, value, title, now()))


def liked(user_id: int, limit: int = 3) -> list[dict]:
    """The titles this account liked or loved most recently: what "Because you liked" rows start from."""
    with connect() as c:
        return rows(c.execute("SELECT * FROM video_ratings WHERE user_id=? AND rating > 0 ORDER BY rated_at DESC, rowid DESC"
                              " LIMIT ?", (user_id, limit)))
