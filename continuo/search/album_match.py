"""Matching untagged library albums to MusicBrainz release groups by title, for one artist at a
time (the artist page asks): "Windswept Adan" in the library is a release of アダンの風, which a
title comparison can't see. Each album is looked up once (MusicBrainz allows ~1 call a second);
misses are retried after a month. Matches feed _LibraryIndex, so availability sees them."""
from datetime import timedelta

from ..db import identity
from ..db import library as lib_db
from ..db.conn import seconds_since
from ..requests.normalise import normalise, strip_edition
from ..upstream import musicbrainz

_PER_CALL = 5
_RETRY_MISS = timedelta(days=30).total_seconds()


def _title(t) -> str:
    return normalise(strip_edition(t))


async def match_artist(mb_artist_id: str, names: set[str], catalog: list[dict]) -> int:
    """Look up library albums by [names] that have no release group and match no catalogue
    title; returns how many were looked up."""
    keys = {normalise(n) for n in names if n}
    titles = {_title(c.get("title")) for c in catalog}
    known = identity.album_matches()
    todo = []
    for a in lib_db.all_albums():
        if normalise(a.get("artist")) not in keys or a.get("mbz_release_group_id") or _title(a["name"]) in titles:
            continue
        m = known.get(a["navidrome_id"])
        if m and (m["mb_release_group_id"] or (seconds_since(m["matched_at"]) or 0) < _RETRY_MISS):
            continue
        todo.append(a)
    for a in todo[:_PER_CALL]:
        identity.set_album_match(a["navidrome_id"], await musicbrainz.release_group_for_title(mb_artist_id, a["name"]))
    return min(len(todo), _PER_CALL)
