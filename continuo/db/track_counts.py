"""album_track_counts: how many songs each release group has (see the migration)."""
from .conn import connect, now


def all_counts() -> dict[str, int]:
    with connect() as c:
        return {r[0]: r[1] for r in c.execute("SELECT release_group_id, track_count FROM album_track_counts")}


def put(release_group_id: str, track_count: int) -> None:
    with connect() as c:
        c.execute("INSERT INTO album_track_counts (release_group_id, track_count, checked_at) VALUES (?, ?, ?) "
                  "ON CONFLICT (release_group_id) DO UPDATE SET track_count=excluded.track_count, "
                  "checked_at=excluded.checked_at", (release_group_id, track_count, now()))
