"""The catalogue artist page (`GET /v1/catalog/artists/{mbid}`): what a library artist's page shows,
for any artist — name, picture, biography, top songs and similar artists — each song and artist
marked against the library. The discography stays on `/albums`.

Every part is best-effort and cached: Lidarr's metadata server (name, picture, overview, albums),
ListenBrainz (similar artists; top songs with LISTENBRAINZ_TOKEN), Wikipedia via Wikidata (the
biography when Lidarr has none). `sources` says which parts answered, so a client can tell
"no top songs" from "top songs unavailable"."""
import asyncio
from typing import Optional

import httpx

from .. import artwork
from ..db import library as lib_db
from ..db import requests as req_db
from ..requests.normalise import album_key, normalise
from ..upstream import lidarr, listenbrainz, wikipedia
from .cache import TTLCache, cached
from .merge import _LibraryIndex, _request_availability, on_server

# Artist facts change slowly; the page is opened often (every tap on an artist).
_cache = TTLCache(ttl=6 * 3600, max_items=256)


async def _upstream(mbid: str) -> dict:
    meta, lb_meta, similar, top = await asyncio.gather(
        lidarr.metadata_artist(mbid),
        listenbrainz.artist_metadata(mbid),
        listenbrainz.similar_artists(mbid),
        listenbrainz.top_recordings(mbid),
    )
    bio = None
    overview = ((meta or {}).get("overview") or "").strip()
    if overview:
        bio = {"text": overview, "source": "lidarr", "url": None}
    else:
        wiki = await wikipedia.summary(((lb_meta or {}).get("rels") or {}).get("wikidata"))
        if wiki:
            bio = {"text": wiki["text"], "source": "wikipedia", "url": wiki["url"]}
    return {"meta": meta, "lb_meta": lb_meta, "similar": similar, "top": top, "bio": bio}


def _top_track(t: dict, albums_by_title: dict[str, dict], index: _LibraryIndex,
               requests: dict[str, dict], artist_mbid: Optional[str] = None) -> dict:
    """One top song with its album: matched to the artist's release groups by title, and to the
    library by release, release group or name."""
    album = albums_by_title.get(normalise(t.get("releaseName")))
    rg = (album or {}).get("musicBrainzReleaseGroupId")
    lib = index.find(rg, [t["releaseMbid"]] if t.get("releaseMbid") else [],
                     album_key(t.get("artist"), t.get("releaseName")), t.get("releaseName"), artist_mbid)
    mine = index.mine(lib) if lib is not None else None
    if lib is not None and mine is None:
        availability = {**on_server(lib), "via": "album"}
    elif lib is not None:
        availability = {"state": "album_in_library", "navidromeAlbumId": mine}
    elif rg and rg in requests:
        availability = _request_availability(requests[rg])
    elif rg:
        availability = {"state": "requestable", "via": "album"}
    else:
        availability = {"state": "catalog"}
    rec = t.get("recordingMbid")
    return {
        "kind": "track", "key": f"mb:rec:{rec}" if rec else f"cat:{normalise(t['title'])}",
        "title": t["title"], "artist": t.get("artist"), "durationMs": t.get("durationMs"),
        "album": {"title": t.get("releaseName"), "musicBrainzReleaseGroupId": rg,
                  "navidromeAlbumId": mine},
        "availability": availability,
    }


def _library_artist_names() -> set[str]:
    return {normalise(a.get("artist")) for a in lib_db.all_albums() if a.get("artist")}


async def build(mbid: str, user_id: Optional[int] = None) -> Optional[dict]:
    """The page for artist [mbid]; None when no source knows the artist."""
    up = await cached(_cache, mbid, lambda: _upstream(mbid))
    meta, lb_meta = up["meta"] or {}, up["lb_meta"] or {}
    name = meta.get("artistname") or meta.get("artistName") or lb_meta.get("name")
    if not name:
        _cache.drop(mbid)  # don't keep a miss for 6 h
        return None
    library_names = _library_artist_names()

    top_tracks = None
    if up["top"] is not None:
        index = _LibraryIndex(lib_db.all_albums(), user_id)
        requests = req_db.active_by_release_group()
        albums_by_title = {}
        try:
            albums = await lidarr.artist_albums(mbid)
        except httpx.HTTPError:
            albums = []  # songs without a release group can't be requested, but still show
        for a in albums:
            albums_by_title.setdefault(normalise(a.get("title")), a)
        top_tracks = [_top_track(t, albums_by_title, index, requests, mbid) for t in up["top"]]

    similar = None
    if up["similar"] is not None:
        similar = [{
            "kind": "artist", "key": f"mb:artist:{a['musicBrainzArtistId']}", "name": a["name"],
            "comment": a.get("comment"), "artwork": {"url": None},
            "ids": {"musicBrainzArtistId": a["musicBrainzArtistId"], "navidromeArtistId": None},
            "availability": {"state": "in_library" if normalise(a["name"]) in library_names else "catalog"},
        } for a in up["similar"]]

    return {
        "kind": "artist", "key": f"mb:artist:{mbid}", "name": name,
        "disambiguation": meta.get("disambiguation") or None,
        "type": meta.get("type") or None,
        "genres": meta.get("genres") or [],
        "artwork": artwork.catalog_art(lidarr.metadata_image(meta)),
        "biography": up["bio"],
        "ids": {"musicBrainzArtistId": mbid, "navidromeArtistId": None},
        "availability": {"state": "in_library" if normalise(name) in library_names else "catalog"},
        "topTracks": top_tracks or [],
        "similarArtists": similar or [],
        "sources": {
            "topTracks": "needs_token" if not listenbrainz.has_token() else ("ok" if up["top"] is not None else "unavailable"),
            "similarArtists": "ok" if up["similar"] is not None else "unavailable",
        },
    }
