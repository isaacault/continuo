"""Which library albums are complete: each store album's song count against its release group's
(MusicBrainz), so the apps can list partial albums apart (GET /v1/library/partial)."""
import logging
from datetime import timedelta

from ..db import identity, track_counts
from ..db import library as lib_db
from ..db.conn import seconds_since
from ..library.singles import is_singles

log = logging.getLogger("continuo")

# A few each tick: MusicBrainz is paced to a call a second, and the library is counted once.
BATCH = 15
# Untagged albums matched by name first (most libraries' albums carry no MusicBrainz tags).
MATCH_BATCH = 10
_RETRY_MISS = timedelta(days=30).total_seconds()


def unmatched(albums: list[dict], matches: dict[str, dict]) -> list[dict]:
    """Store albums with no release group tag, not single songs, not looked up (or a miss a month old)."""
    out = []
    for a in albums:
        if a.get("mbz_release_group_id") or is_singles(a.get("folder")) or not a.get("artist") or not a.get("name"):
            continue
        m = matches.get(a["navidrome_id"])
        if m and (m["mb_release_group_id"] or (seconds_since(m["matched_at"]) or 0) < _RETRY_MISS):
            continue
        out.append(a)
    return out


def _with_matches(albums: list[dict]) -> list[dict]:
    """Albums as the library index sees them: a match counts as a tag."""
    matches = identity.album_matches()
    return [a if a.get("mbz_release_group_id") or not (m := matches.get(a["navidrome_id"])) or not m["mb_release_group_id"]
            else {**a, "mbz_release_group_id": m["mb_release_group_id"]} for a in albums]


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
    looked = unmatched(lib_db.all_albums(), identity.album_matches())[:MATCH_BATCH]
    for a in looked:
        identity.set_album_match(a["navidrome_id"], await musicbrainz.release_group_by_names(a["artist"], a["name"]))
    if looked:
        log.info("album completeness: looked up %d untagged album(s)", len(looked))
    todo = uncounted(_with_matches(lib_db.all_albums()), track_counts.all_counts())[:BATCH]
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
