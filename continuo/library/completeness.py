"""Which library albums are complete: each store album's song count against its release group's
(MusicBrainz), so the apps can list partial albums apart (GET /v1/library/partial)."""
import logging

from ..db import library as lib_db
from ..db import track_counts
from ..library.singles import is_singles

log = logging.getLogger("continuo")

# A few each tick: MusicBrainz is paced to a call a second, and the library is counted once.
BATCH = 15


def uncounted(albums: list[dict], counts: dict[str, int]) -> list[str]:
    """The release groups of the store's albums (not single songs) not counted yet, in order."""
    out: list[str] = []
    for a in albums:
        rg = a.get("mbz_release_group_id")
        if rg and rg not in counts and rg not in out and not is_singles(a.get("folder")):
            out.append(rg)
    return out


async def tick() -> None:
    from ..upstream import musicbrainz
    todo = uncounted(lib_db.all_albums(), track_counts.all_counts())[:BATCH]
    for rg in todo:
        try:
            tracks = await musicbrainz.album_tracks(rg, strict=True)
        except musicbrainz.Unavailable:
            return          # MusicBrainz busy or down: next tick
        track_counts.put(rg, len(tracks))
    if todo:
        log.info("album completeness: counted %d release group(s)", len(todo))


def partial(albums: list[dict], counts: dict[str, int]) -> list[dict]:
    """Store albums with fewer songs than their release group: [{album, have, expected}]."""
    out = []
    for a in albums:
        if is_singles(a.get("folder")):
            continue
        expected = counts.get(a.get("mbz_release_group_id") or "")
        have = int(a.get("song_count") or 0)
        if expected and 0 < have < expected:
            out.append({"album": a, "have": have, "expected": expected})
    return out
