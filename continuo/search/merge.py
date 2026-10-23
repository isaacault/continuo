"""Unified search: the library (Navidrome search3, as the user) and the catalogue (Lidarr lookup +
MusicBrainz recordings) merged into one list, each hit carrying an `availability`.

Merge key: the MusicBrainz release-group when known, else normalised artist|title. Library hits
win; a catalogue hit that resolves to a library album folds into it.
"""
import asyncio
import logging
import unicodedata
from typing import Optional

import httpx

from .. import artwork, config
from ..db import library as lib_db
from ..db import requests as req_db
from ..requests.normalise import album_key, normalise, track_key
from ..upstream import lidarr, musicbrainz, navidrome
from .cache import TTLCache, cached

log = logging.getLogger("continuo")

_catalog_cache = TTLCache(600.0)

ALL_TYPES = ("album", "artist", "track")


def _norm_term(q: str) -> str:
    return " ".join(q.casefold().split())


def cover_art(cover_id: Optional[str]) -> dict:
    """Library artwork. The client appends its own Subsonic auth (the façade credential)."""
    if not cover_id:
        return {"url": None}
    return {"url": f"{config.settings.public_base_url}/rest/getCoverArt?id={cover_id}",
            "auth": "subsonic"}


def _request_availability(req: dict) -> dict:
    state = "requested" if req["status"] == "pending" else req["status"]
    if state == "error":
        state = "requestable"
    out = {"state": state, "requestId": req["id"]}
    if req.get("still_looking"):
        out["stillLooking"] = True
    if req["status"] == "available" and req.get("navidrome_album_id"):
        out = {"state": "in_library", "navidromeAlbumId": req["navidrome_album_id"]}
    return out


# Characters normalise() drops that still make two titles different albums ("0" vs "0%"). Symbols
# (Unicode S*) count too, except "+", which normalise() reads as "and".
_MARKS = set("%#$@*=~^°±§")


def _marks(title: Optional[str]) -> str:
    return "".join(sorted(ch for ch in (title or "")
                          if ch != "+" and (ch in _MARKS or unicodedata.category(ch).startswith("S"))))


def same_by_name(lib_rg: Optional[str], lib_title: Optional[str], rg: Optional[str],
                 title: Optional[str]) -> bool:
    """Whether a library album that shares a catalogue release's normalised artist|title key is
    that release. Not when both have release groups and they differ (a "Magpie" single is not the
    "Magpie" album), nor when the titles differ by a mark normalise() drops ("0" is not "0%")."""
    if rg and lib_rg and rg != lib_rg:
        return False
    return title is None or _marks(title) == _marks(lib_title)


class _LibraryIndex:
    """Lookups over library_albums (the whole library, not just this query's search3 page)."""

    def __init__(self, albums: list[dict]):
        # Untagged albums matched to a release group by title (db.identity) count as tagged.
        from ..db import identity
        matches = identity.album_matches()
        # mb artist id -> the library's name for that artist, where it differs (Ichiko Aoba).
        self.library_names = identity.library_names()
        albums = [a if a.get("mbz_release_group_id") or not (m := matches.get(a["navidrome_id"])) or not m["mb_release_group_id"]
                  else {**a, "mbz_release_group_id": m["mb_release_group_id"]} for a in albums]
        self.by_id = {a["navidrome_id"]: a for a in albums}
        self.by_rg, self.by_release, self.by_key = {}, {}, {}
        for a in albums:
            if a.get("mbz_release_group_id"):
                self.by_rg.setdefault(a["mbz_release_group_id"], a)
            if a.get("mbz_album_id"):
                self.by_release.setdefault(a["mbz_album_id"], a)
            if a.get("norm_key"):
                self.by_key.setdefault(a["norm_key"], []).append(a)

    def alias_key(self, artist_mbid: Optional[str], title: Optional[str]) -> Optional[str]:
        """The artist|title key under the library's own name for the artist, when it has one."""
        name = self.library_names.get(artist_mbid) if artist_mbid else None
        return album_key(name, title) if name and title is not None else None

    def find(self, rg: Optional[str], release_ids: list[str], key: str,
             title: Optional[str] = None, artist_mbid: Optional[str] = None) -> Optional[dict]:
        if rg and rg in self.by_rg:
            return self.by_rg[rg]
        for rid in release_ids:
            if rid in self.by_release:
                return self.by_release[rid]
        for k in (key, self.alias_key(artist_mbid, title)):
            for a in self.by_key.get(k, ()) if k else ():
                if same_by_name(a.get("mbz_release_group_id"), a.get("name"), rg, title):
                    return a
        return None


def merge_albums(lib_albums: list[dict], catalog: list[dict], index: _LibraryIndex,
                 requests: dict[str, dict], limit: int) -> list[dict]:
    results: list[dict] = []
    by_nd: dict[str, dict] = {}

    for a in lib_albums:
        indexed = index.by_id.get(a["id"], {})
        rg = indexed.get("mbz_release_group_id")
        hit = {
            "kind": "album",
            "key": f"mb:rg:{rg}" if rg else f"nd:{a['id']}",
            "title": a.get("name"), "artist": a.get("artist"), "year": a.get("year") or None,
            "albumType": None,
            "artwork": cover_art(a.get("coverArt")),
            "ids": {"musicBrainzReleaseGroupId": rg, "musicBrainzReleaseId": a.get("musicBrainzId") or None,
                    "navidromeAlbumId": a["id"], "lidarrAlbumId": None},
            "availability": {"state": "in_library", "navidromeAlbumId": a["id"],
                             "trackCount": a.get("songCount")},
            "_key": album_key(a.get("artist"), a.get("name")),
            "_release": a.get("musicBrainzId") or None,
        }
        by_nd[a["id"]] = hit
        results.append(hit)

    for c in catalog:
        rg = c.get("musicBrainzReleaseGroupId")
        key = album_key(c.get("artist"), c.get("title"))
        keys = {key, index.alias_key(c.get("musicBrainzArtistId"), c.get("title"))} - {None}
        # Fold into a library hit of this query…
        target = None
        for hit in results:
            if hit["ids"]["navidromeAlbumId"] is None:
                continue
            if (rg and hit["ids"]["musicBrainzReleaseGroupId"] == rg) \
                    or (hit["_release"] and hit["_release"] in c.get("releaseIds", [])) \
                    or (hit["_key"] in keys and same_by_name(hit["ids"]["musicBrainzReleaseGroupId"],
                                                            hit.get("title"), rg, c.get("title"))):
                target = hit
                break
        # …or into a library album the search page didn't return.
        if target is None:
            lib = index.find(rg, c.get("releaseIds", []), key, c.get("title"), c.get("musicBrainzArtistId"))
            if lib is not None:
                target = by_nd.get(lib["navidrome_id"])
                if target is None:
                    target = {
                        "kind": "album", "key": f"mb:rg:{rg}" if rg else f"nd:{lib['navidrome_id']}",
                        "title": lib["name"], "artist": lib.get("artist"), "year": lib.get("year"),
                        "albumType": c.get("albumType"),
                        "artwork": cover_art(lib["navidrome_id"]),
                        "ids": {"musicBrainzReleaseGroupId": rg,
                                "musicBrainzReleaseId": lib.get("mbz_album_id"),
                                "navidromeAlbumId": lib["navidrome_id"], "lidarrAlbumId": None},
                        "availability": {"state": "in_library",
                                         "navidromeAlbumId": lib["navidrome_id"],
                                         "trackCount": lib.get("song_count")},
                        "_key": key, "_release": lib.get("mbz_album_id"),
                    }
                    by_nd[lib["navidrome_id"]] = target
                    results.append(target)
        if target is not None:
            ids = target["ids"]
            ids["musicBrainzReleaseGroupId"] = ids["musicBrainzReleaseGroupId"] or rg
            ids["lidarrAlbumId"] = ids["lidarrAlbumId"] or c.get("lidarrAlbumId")
            if rg and target["key"].startswith("nd:"):
                target["key"] = f"mb:rg:{rg}"
            target["albumType"] = target["albumType"] or c.get("albumType")
            have = target["availability"].get("trackCount")
            expected = c.get("trackCount") if c.get("owned") else None
            if have and expected and have < expected:
                target["availability"] = {"state": "partial", "have": have, "expected": expected,
                                          "navidromeAlbumId": ids["navidromeAlbumId"]}
            continue

        req = requests.get(rg) if rg else None
        availability = _request_availability(req) if req else {"state": "requestable"}
        if availability["state"] == "requestable" and c.get("owned"):
            # Lidarr has the files but Navidrome doesn't show the album (yet).
            availability = {"state": "importing"}
        results.append({
            "kind": "album", "key": f"mb:rg:{rg}" if rg else f"cat:{key}",
            "title": c.get("title"), "artist": c.get("artist"), "year": c.get("year"),
            "albumType": c.get("albumType"),
            "artwork": artwork.catalog_art(c.get("imageUrl")),
            "ids": {"musicBrainzReleaseGroupId": rg, "musicBrainzReleaseId": None,
                    "navidromeAlbumId": None, "lidarrAlbumId": c.get("lidarrAlbumId")},
            "availability": availability,
            "_key": key, "_release": None,
        })

    # One row per merge key (two catalogue editions folding the same way).
    seen, out = set(), []
    for r in results:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        r.pop("_key", None)
        r.pop("_release", None)
        out.append(r)
    return out[:limit]


def merge_artists(lib_artists: list[dict], catalog: list[dict], limit: int) -> list[dict]:
    results, by_mbid, by_name = [], {}, {}
    for a in lib_artists:
        mbid = a.get("musicBrainzId") or None
        hit = {
            "kind": "artist", "key": f"mb:artist:{mbid}" if mbid else f"nd:artist:{a['id']}",
            "name": a.get("name"), "artwork": cover_art(a.get("coverArt")),
            "ids": {"musicBrainzArtistId": mbid, "navidromeArtistId": a["id"]},
            "availability": {"state": "in_library", "albumCount": a.get("albumCount")},
        }
        results.append(hit)
        if mbid:
            by_mbid[mbid] = hit
        by_name.setdefault(normalise(a.get("name")), hit)
    # The library's own name for an artist MusicBrainz names differently (Ichiko Aoba / 青葉市子).
    from ..db import identity
    library_names = identity.library_names() if catalog else {}
    for c in catalog:
        mbid = c.get("musicBrainzArtistId")
        hit = by_mbid.get(mbid) if mbid else None
        hit = hit or by_name.get(normalise(c.get("name")))
        if hit is None and mbid in library_names:
            hit = by_name.get(normalise(library_names[mbid]))
        if hit is not None:
            hit["ids"]["musicBrainzArtistId"] = hit["ids"]["musicBrainzArtistId"] or mbid
            if hit["key"].startswith("nd:") and mbid:
                hit["key"] = f"mb:artist:{mbid}"
            continue
        # Not requestable as a whole (policy: nothing follows artists); browse the discography.
        results.append({
            "kind": "artist", "key": f"mb:artist:{mbid}" if mbid else f"cat:{normalise(c.get('name'))}",
            "name": c.get("name"), "artwork": artwork.catalog_art(c.get("imageUrl")),
            "ids": {"musicBrainzArtistId": mbid, "navidromeArtistId": None},
            "availability": {"state": "catalog"},
        })
    return results[:limit]


def merge_tracks(lib_songs: list[dict], catalog: list[dict], index: _LibraryIndex,
                 requests: dict[str, dict], limit: int) -> list[dict]:
    results, by_rec, by_key = [], {}, {}
    for s in lib_songs:
        rec = s.get("musicBrainzId") or None
        hit = {
            "kind": "track", "key": f"mb:rec:{rec}" if rec else f"nd:track:{s['id']}",
            "title": s.get("title"), "artist": s.get("artist"), "durationMs":
                (s.get("duration") or 0) * 1000 or None,
            "album": {"title": s.get("album"), "navidromeAlbumId": s.get("albumId"),
                      "musicBrainzReleaseGroupId": None},
            "artwork": cover_art(s.get("coverArt")),
            "availability": {"state": "in_library", "navidromeTrackId": s["id"]},
        }
        results.append(hit)
        if rec:
            by_rec[rec] = hit
        by_key.setdefault(track_key(s.get("artist"), s.get("title")), hit)
    for c in catalog:
        rec = c.get("musicBrainzRecordingId")
        hit = by_rec.get(rec) if rec else None
        hit = hit or by_key.get(track_key(c.get("artist"), c.get("title")))
        rg = c["album"]["musicBrainzReleaseGroupId"]
        if hit is not None:
            hit["album"]["musicBrainzReleaseGroupId"] = rg
            continue
        lib = index.by_rg.get(rg)
        if lib is not None:
            availability = {"state": "album_in_library", "navidromeAlbumId": lib["navidrome_id"]}
        elif rg in requests:
            availability = _request_availability(requests[rg])
        else:
            availability = {"state": "requestable", "via": "album"}
        results.append({
            "kind": "track", "key": f"mb:rec:{rec}", "title": c.get("title"),
            "artist": c.get("artist"), "durationMs": c.get("durationMs"),
            "album": dict(c["album"], navidromeAlbumId=lib["navidrome_id"] if lib else None),
            "artwork": artwork.catalog_art(c.get("imageUrl")),
            "availability": availability,
        })
    return results[:limit]


async def _catalog_albums(q: str) -> list[dict]:
    return await cached(_catalog_cache, ("album", _norm_term(q)), lambda: lidarr.search_albums(q))


async def _catalog_artists(q: str) -> list[dict]:
    return await cached(_catalog_cache, ("artist", _norm_term(q)), lambda: lidarr.search_artists(q))


async def _add_renamed_library_artists(library: dict, catalog: list[dict], navidrome_user: str) -> None:
    """Searching フィッシュマンズ finds the catalogue artist, but the library knows them as
    Fishmans (artist_names), so Navidrome's own search missed them: fetch those library artists
    by their library name, so merge_artists folds the two into one."""
    from ..db import identity
    names = identity.library_names()
    have = {normalise(a.get("name")) for a in library.get("artist") or []}
    wanted = []
    for c in catalog:
        name = names.get(c.get("musicBrainzArtistId"))
        if name and normalise(name) not in have and name not in wanted:
            wanted.append(name)
    for name in wanted[:3]:
        try:
            found = await navidrome.search3(name, navidrome_user, artists=3, albums=0, songs=0)
        except Exception:  # noqa: BLE001 — the catalogue result still shows
            continue
        for a in found.get("artist") or []:
            if normalise(a.get("name")) == normalise(name):
                library.setdefault("artist", []).append(a)
                have.add(normalise(name))
                break


async def unified_search(q: str, types: tuple[str, ...], limit: int,
                         navidrome_user: str) -> dict:
    want = set(types)

    async def none():
        return []

    library_t = navidrome.search3(q, navidrome_user,
                                  artists=limit if "artist" in want else 0,
                                  albums=limit if "album" in want else 0,
                                  songs=limit if "track" in want else 0)
    tasks = [
        library_t,
        _catalog_albums(q) if "album" in want else none(),
        _catalog_artists(q) if "artist" in want else none(),
        musicbrainz.search_tracks(q, limit) if "track" in want else none(),
    ]
    library, cat_albums, cat_artists, cat_tracks = await asyncio.gather(*tasks,
                                                                        return_exceptions=True)
    sources = {"library": "ok", "catalog": "ok"}
    if isinstance(library, BaseException):
        log.warning("search: library unavailable: %s", library)
        sources["library"] = "unavailable"
        library = {"artist": [], "album": [], "song": []}
    failed = []
    for name, val in (("album", cat_albums), ("artist", cat_artists), ("track", cat_tracks)):
        if isinstance(val, BaseException):
            if not isinstance(val, (httpx.HTTPError, ValueError)):
                raise val
            log.warning("search: catalogue %s lookup failed: %s", name, val)
            failed.append(name)
    asked = [t for t in ("album", "artist", "track") if t in want]
    if failed:
        sources["catalog"] = "unavailable" if set(failed) >= set(asked) else "degraded"
    cat_albums = [] if isinstance(cat_albums, BaseException) else cat_albums
    cat_artists = [] if isinstance(cat_artists, BaseException) else cat_artists
    cat_tracks = [] if isinstance(cat_tracks, BaseException) else cat_tracks

    if "artist" in want and cat_artists and not isinstance(library, BaseException):
        await _add_renamed_library_artists(library, cat_artists, navidrome_user)

    index = _LibraryIndex(lib_db.all_albums())
    requests = req_db.active_by_release_group()
    # Available requests too: an album just made available but not yet indexed still resolves.
    for r in req_db.list_requests(statuses=("available",), limit=200):
        requests.setdefault(r["mb_release_group_id"], r)

    results: list[dict] = []
    if "album" in want:
        results += merge_albums(library["album"], cat_albums, index, requests, limit)
    if "artist" in want:
        results += merge_artists(library["artist"], cat_artists, limit)
    if "track" in want:
        results += merge_tracks(library["song"], cat_tracks, index, requests, limit)
    return {"query": q, "sources": sources, "results": results}
