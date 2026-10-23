"""Which catalogue artist a library artist is, when the files carry no MusicBrainz id.

Names are ambiguous and they differ:
- MusicBrainz has four artists called "Peach Pit" (a Canadian indie band, a Croatian math-rock
  band, a manga duo, a Detroit rock band); the first name match showed the wrong band's albums.
- The library's "Ichiko Aoba" is 青葉市子 in MusicBrainz; "Ichiko Aoba" is only an alias there.

So a candidate from Lidarr's artist search counts when its name or one of its aliases is the
library name, or when its discography shares an album title with the library's albums by that
name. Among those: one already in Lidarr wins (that's whose albums were requested), then the most
shared albums, then a name or alias match, then Lidarr's order. When the winner's name isn't the
library's, the pairing is remembered (db.identity): search folds the catalogue artist into the
library one, and albums Lidarr imports for it get the library's name."""
import asyncio
from typing import Optional

import httpx

from ..db import identity
from ..db import library as lib_db
from ..requests.normalise import normalise, strip_edition
from ..upstream import lidarr
from .cache import TTLCache, cached
from .merge import _catalog_artists

_MAX_CANDIDATES = 6
_facts_cache = TTLCache(ttl=6 * 3600, max_items=512)


def _title(t: Optional[str]) -> str:
    return normalise(strip_edition(t))


async def artist_facts(mbid: str) -> dict:
    """{name, titles, aliases} of a catalogue artist, normalised; empty when the metadata server
    failed."""
    async def fetch() -> dict:
        data = await lidarr.metadata_artist(mbid) or {}
        return {
            "name": data.get("artistname") or data.get("artistName"),
            "titles": {_title(a.get("Title") or a.get("title")) for a in data.get("Albums") or data.get("albums") or []},
            "aliases": {normalise(a) for a in data.get("artistaliases") or data.get("artistAliases") or [] if a},
        }
    return await cached(_facts_cache, mbid, fetch)


def library_titles(name: str) -> set[str]:
    key = normalise(name)
    return {_title(a["name"]) for a in lib_db.all_albums() if normalise(a.get("artist")) == key}


async def resolve(name: str) -> Optional[dict]:
    """{musicBrainzArtistId, name, sharedAlbums, inLidarr} for the library artist [name]; None
    when no catalogue artist is it."""
    key = normalise(name)
    if not key:
        return None
    found = [a for a in await _catalog_artists(name) if a.get("musicBrainzArtistId")][:_MAX_CANDIDATES]
    if not found:
        return None
    library = library_titles(name)

    async def judge(a: dict) -> Optional[tuple]:
        facts = await artist_facts(a["musicBrainzArtistId"])
        named = normalise(a.get("name")) == key or key in facts["aliases"]
        shared = len(library & facts["titles"])
        if not named and not shared:
            return None
        try:
            in_lidarr = await lidarr.find_library_artist(a["musicBrainzArtistId"]) is not None
        except httpx.HTTPError:
            in_lidarr = False
        return in_lidarr, shared, named

    judged = await asyncio.gather(*(judge(a) for a in found))
    ranked = [(j, a) for j, a in zip(judged, found, strict=True) if j is not None]
    if not ranked:
        return None
    # max() keeps the first of equals: Lidarr's order breaks ties.
    (in_lidarr, shared, _named), best = max(ranked, key=lambda p: p[0])
    if normalise(best.get("name")) != key:
        identity.set_library_name(best["musicBrainzArtistId"], name)
    return {"musicBrainzArtistId": best["musicBrainzArtistId"], "name": best["name"],
            "sharedAlbums": shared, "inLidarr": in_lidarr}


async def library_name_for(mb_artist_id: Optional[str], catalogue_name: Optional[str]) -> Optional[str]:
    """The library's own name for a catalogue artist when it differs (Ichiko Aoba for 青葉市子):
    remembered, else the one library artist whose name is among the catalogue artist's aliases."""
    if not mb_artist_id:
        return None
    known = identity.library_name(mb_artist_id)
    if known:
        return known
    aliases = (await artist_facts(mb_artist_id))["aliases"]
    if not aliases:
        return None
    by_key = {}
    for a in lib_db.all_albums():
        if a.get("artist"):
            by_key.setdefault(normalise(a["artist"]), a["artist"])
    if normalise(catalogue_name) in by_key:
        return None  # the library already uses the catalogue's name
    hits = {by_key[k] for k in aliases if k in by_key}
    if len(hits) != 1:
        return None
    name = hits.pop()
    identity.set_library_name(mb_artist_id, name)
    return name
