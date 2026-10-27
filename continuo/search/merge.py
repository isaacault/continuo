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


def on_server(lib: dict) -> dict:
    """A store album this account's library doesn't have (#27): Add puts it there, instantly."""
    out = {"state": "on_server", "storeAlbumId": lib["navidrome_id"]}
    return {**out, "trackCount": lib["song_count"]} if lib.get("song_count") else out


class _LibraryIndex:
    """Lookups over library_albums (the whole library, not just this query's search3 page).

    With [user_id] whose account has its own library (#27), a store album it doesn't have is
    "on_server" (mine() is None), and one it has opens with its own library's album id."""

    def __init__(self, albums: list[dict], user_id: Optional[int] = None):
        from ..library.ownership import Ownership
        from ..library.singles import Singles
        self.own = Ownership(user_id)
        self.singles = Singles()
        # Untagged albums matched to a release group by title (db.identity) count as tagged.
        from ..db import identity
        matches = identity.album_matches()
        # mb artist id -> the library's name for that artist, where it differs (Ichiko Aoba).
        self.library_names = identity.library_names()
        albums = [a if a.get("mbz_release_group_id") or not (m := matches.get(a["navidrome_id"])) or not m["mb_release_group_id"]
                  else {**a, "mbz_release_group_id": m["mb_release_group_id"]} for a in albums]
        self.by_id = {a["navidrome_id"]: a for a in albums}
        if self.own.enabled:
            # The account's copies (what its own search3 returns) know what the store album knows.
            store_by_folder = {a["folder"]: a for a in albums if a.get("folder")}
            for folder, nd_id in self.own.ids.items():
                s = store_by_folder.get(folder, {})
                self.by_id[nd_id] = {**s, "navidrome_id": nd_id}
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

    def album_view(self, album_id: Optional[str], availability: dict) -> dict:
        """[availability] of the library album [album_id]; one made only of single songs (#24)
        is partial and still requestable."""
        from ..library.singles import is_singles
        lib = self.by_id.get(album_id) if album_id else None
        if lib and is_singles(lib.get("folder")):
            return self.singles.availability(lib, album_id)
        return availability

    def song_album(self, lib: Optional[dict], recording_id: Optional[str]) -> Optional[dict]:
        """The library album a catalogue song is in: none when it's a singles folder that
        doesn't hold this song (the album's other songs are still requestable)."""
        from ..library.singles import is_singles
        if lib and is_singles(lib.get("folder")) and recording_id not in self.singles.recordings:
            return None
        return lib

    def mine(self, lib: dict) -> Optional[str]:
        """The album id this account opens store album [lib] with; None when it's only on the
        server (another account has it)."""
        if not self.own.enabled:
            return lib["navidrome_id"]
        return self.own.ids.get(lib.get("folder")) if lib.get("folder") in self.own.mine else None

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
            mine = index.mine(lib) if lib is not None else None
            if lib is not None and mine is None:
                results.append({
                    "kind": "album", "key": f"mb:rg:{rg}" if rg else f"nd:{lib['navidrome_id']}",
                    "title": c.get("title"), "artist": c.get("artist"), "year": c.get("year"),
                    "albumType": c.get("albumType"),
                    "artwork": artwork.catalog_art(c.get("imageUrl")),
                    "ids": {"musicBrainzReleaseGroupId": rg, "musicBrainzReleaseId": None,
                            "navidromeAlbumId": None, "lidarrAlbumId": c.get("lidarrAlbumId")},
                    "availability": on_server(lib),
                    "_key": key, "_release": None,
                })
                continue
            if lib is not None:
                target = by_nd.get(mine)
                if target is None:
                    target = {
                        "kind": "album", "key": f"mb:rg:{rg}" if rg else f"nd:{mine}",
                        "title": lib["name"], "artist": lib.get("artist"), "year": lib.get("year"),
                        "albumType": c.get("albumType"),
                        "artwork": cover_art(mine),
                        "ids": {"musicBrainzReleaseGroupId": rg,
                                "musicBrainzReleaseId": lib.get("mbz_album_id"),
                                "navidromeAlbumId": mine, "lidarrAlbumId": None},
                        "availability": {"state": "in_library",
                                         "navidromeAlbumId": mine,
                                         "trackCount": lib.get("song_count")},
                        "_key": key, "_release": lib.get("mbz_album_id"),
                    }
                    by_nd[mine] = target
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
    for hit in out:
        hit["availability"] = index.album_view(hit["ids"]["navidromeAlbumId"], hit["availability"])
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
                 requests: dict[str, dict], limit: int,
                 track_requests: Optional[dict[str, dict]] = None) -> list[dict]:
    """[track_requests]: live single-song requests by recording MBID (#24)."""
    track_requests = track_requests or {}
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
        lib = index.song_album(index.by_rg.get(rg), rec)
        mine = index.mine(lib) if lib is not None else None
        if lib is not None and mine is None:
            availability = {**on_server(lib), "via": "album"}
            lib = None
        elif lib is not None:
            availability = {"state": "album_in_library", "navidromeAlbumId": mine}
        elif rg in requests:
            availability = _request_availability(requests[rg])
        elif rec and rec in track_requests:
            availability = _request_availability(track_requests[rec])
        else:
            availability = {"state": "requestable", "via": "album"}
        results.append({
            "kind": "track", "key": f"mb:rec:{rec}", "title": c.get("title"),
            "artist": c.get("artist"), "durationMs": c.get("durationMs"),
            "album": dict(c["album"], navidromeAlbumId=mine if lib else None),
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


def _first_then(first: list[dict], rest: list[dict], id_field: str) -> list[dict]:
    seen = {x.get(id_field) for x in first if x.get(id_field)}
    return first + [x for x in rest if not x.get(id_field) or x.get(id_field) not in seen]


async def unified_search(q: str, types: tuple[str, ...], limit: int,
                         navidrome_user: str, user_id: Optional[int] = None) -> dict:
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
        _ranked(q, min(limit, 10)),
    ]
    library, cat_albums, cat_artists, cat_tracks, ranked_cat = await asyncio.gather(*tasks,
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
    # The ranked catalogue first (popular, by title), then MusicBrainz's and Lidarr's for depth.
    ranked_cat = ranked_cat if isinstance(ranked_cat, dict) else {}
    cat_albums = _first_then(ranked_cat.get("albums", []) if "album" in want else [], cat_albums, "musicBrainzReleaseGroupId")
    cat_artists = _first_then(ranked_cat.get("artists", []) if "artist" in want else [], cat_artists, "musicBrainzArtistId")
    cat_tracks = _first_then(ranked_cat.get("tracks", []) if "track" in want else [], cat_tracks, "musicBrainzRecordingId")

    if "artist" in want and cat_artists and not isinstance(library, BaseException):
        await _add_renamed_library_artists(library, cat_artists, navidrome_user)

    index = _LibraryIndex(lib_db.all_albums(), user_id)
    requests = req_db.active_by_release_group()
    # Available requests too: an album just made available but not yet indexed still resolves.
    for r in req_db.list_requests(statuses=("available",), limit=200, kind="album"):
        requests.setdefault(r["mb_release_group_id"], r)

    results: list[dict] = []
    if "album" in want:
        results += merge_albums(library["album"], cat_albums, index, requests, limit)
    if "artist" in want:
        results += merge_artists(library["artist"], cat_artists, limit)
    if "track" in want:
        results += merge_tracks(library["song"], cat_tracks, index, requests, limit, req_db.active_tracks())
    return {"query": q, "sources": sources, "results": results, "ranked": bool(ranked_cat),
            "best": lead(q, results, ranked_cat.get("best"))}


# ---- quick search: the top few, fast, while typing ----------------------------------------------

_quick_cache = TTLCache(600.0, max_items=512)
# MusicBrainz is paced to one call a second and sheds load at busy times: while typing, its artist
# candidates get this long, then the quick search goes with Lidarr's alone.
_QUICK_MB_BUDGET_S = 1.2
# ListenBrainz usually answers in 0.1-0.4 s but now and then hangs to its 15 s timeout: while
# typing, its ranking gets this long, and so do the top artist's albums and songs.
_QUICK_LB_BUDGET_S = 1.5


async def _within(budget: float, coro, fallback):
    """[coro]'s result, or [fallback] (and whether it was the fallback) past [budget] seconds."""
    try:
        return await asyncio.wait_for(coro, budget), False
    except asyncio.TimeoutError:
        return fallback, True


def _caa(release_mbid: Optional[str]) -> Optional[str]:
    return f"https://coverartarchive.org/release/{release_mbid}/front-500" if release_mbid else None


async def _quick_catalog(q: str, limit: int) -> dict:
    """The catalogue part of a quick search, for any account (cached by query): the most listened
    artists matching [q], and the top one's most listened albums and songs.

    Candidates come from MusicBrainz's artist search (which finds "Beyoncé" for "bey") within a
    short budget, and Lidarr's (fast, no rate limit) beside it; only names containing what was
    typed count, and ListenBrainz's listen counts rank them, so "sabrina" is Sabrina Carpenter
    before a dozen artists called Sabrina."""
    from ..upstream import listenbrainz
    want = normalise(q)

    async def from_musicbrainz() -> list[dict]:
        try:
            data = await asyncio.wait_for(musicbrainz._get("/artist", {"query": q, "limit": 15}, retries=0),
                                          _QUICK_MB_BUDGET_S)
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
            return []
        return [{"kind": "artist", "name": a.get("name"), "musicBrainzArtistId": a.get("id"), "imageUrl": None,
                 "lidarrArtistId": None} for a in data.get("artists") or [] if a.get("id") and a.get("name")]

    async def from_lidarr() -> list[dict]:
        try:
            return await _catalog_artists(q)
        except (httpx.HTTPError, ValueError):
            return []

    mb, lid = await asyncio.gather(from_musicbrainz(), from_lidarr())
    pool: dict[str, dict] = {}
    for a in lid + mb:                                     # Lidarr's carry pictures: theirs first
        mbid = a.get("musicBrainzArtistId")
        if mbid and mbid not in pool and want and want in normalise(a.get("name")):
            pool[mbid] = a
    listens, slow = await _within(_QUICK_LB_BUDGET_S, listenbrainz.artist_popularity(list(pool)), {})
    artists = sorted(pool.values(), key=lambda a: -listens.get(a["musicBrainzArtistId"], 0))[:limit]
    if not artists:
        return {"artists": [], "albums": [], "tracks": [], "complete": not slow}
    top = artists[0]
    (albums_raw, slow_albums), (tracks_raw, slow_tracks) = await asyncio.gather(
        _within(_QUICK_LB_BUDGET_S, listenbrainz.top_release_groups(top["musicBrainzArtistId"], limit=limit), None),
        _within(_QUICK_LB_BUDGET_S, listenbrainz.top_recordings(top["musicBrainzArtistId"], limit=limit), None))
    slow = slow or slow_albums or slow_tracks
    albums = [{"kind": "album", "title": a["title"], "artist": top["name"], "year": a["year"],
               "albumType": a["albumType"], "imageUrl": _caa(a["caaReleaseMbid"]),
               "musicBrainzReleaseGroupId": a["musicBrainzReleaseGroupId"], "musicBrainzArtistId": top["musicBrainzArtistId"],
               "releaseIds": [], "owned": False, "trackCount": None, "lidarrAlbumId": None}
              for a in albums_raw or []]
    by_title = {normalise(a["title"]): a for a in albums}
    tracks = []
    for t in tracks_raw or []:
        if not t.get("recordingMbid"):
            continue
        album = by_title.get(normalise(t.get("releaseName")))
        tracks.append({"kind": "track", "musicBrainzRecordingId": t["recordingMbid"], "title": t["title"],
                       "artist": t.get("artist") or top["name"], "durationMs": t.get("durationMs"),
                       "album": {"title": t.get("releaseName"),
                                 "musicBrainzReleaseGroupId": album["musicBrainzReleaseGroupId"] if album else None,
                                 "year": album["year"] if album else None, "albumType": album["albumType"] if album else None},
                       "imageUrl": album["imageUrl"] if album else None})
    return {"artists": artists, "albums": albums, "tracks": tracks, "complete": not slow}


async def _ranked(q: str, limit: int) -> Optional[dict]:
    """search.ranked's catalogue (Deezer's ranking, MusicBrainz IDs), or None when Deezer can't
    answer now (its quota, an outage): the caller uses MusicBrainz's search instead."""
    from ..upstream import deezer
    from . import ranked
    try:
        return await ranked.catalog(q, limit)
    except (deezer.Exhausted, httpx.HTTPError, ValueError) as e:
        log.info("search: ranked catalogue unavailable (%s), MusicBrainz's instead", e.__class__.__name__)
        return None


def lead(q: str, results: list[dict], catalog_best: Optional[str]) -> Optional[str]:
    """The key of the hit the page leads with: one of yours whose name is what was typed (an
    artist or album whose name starts so, a song with exactly that title: "bey" isn't your song
    Beyond the Pine), else the catalogue's best."""
    from .ranked import match
    mine = [r for r in results if (r.get("availability") or {}).get("state") == "in_library"]
    for want in (3.0, 2.0):
        for kind in ("artist", "album", "track"):
            if kind == "track" and want < 3.0:
                continue
            for r in mine:
                if r["kind"] == kind and match(q, r.get("name") or r.get("title")) >= want:
                    return r["key"]
    return catalog_best if catalog_best in {r["key"] for r in results} else None


async def _quick_cached(q: str, limit: int) -> dict:
    """The quick catalogue (ranked, else _quick_catalog), cached by query; one cut short by a
    slow ListenBrainz isn't kept."""
    key = normalise(q)
    hit = _quick_cache.get(key)
    if hit is not None:
        return hit
    ranked_cat = await _ranked(q, limit)
    value = dict(ranked_cat, ranked=True) if ranked_cat is not None else await _quick_catalog(q, limit)
    if value.get("complete", True):
        _quick_cache.put(key, value)
    return value


async def quick_search(q: str, navidrome_user: str, user_id: Optional[int] = None, limit: int = 3) -> dict:
    """The top few, fast, for searching while typing: your library's best matches and the
    catalogue's most listened (see _quick_catalog), in the full search's shape. The full search
    (songs from MusicBrainz, everything else) is for pressing search or More results."""
    library_t = navidrome.search3(q, navidrome_user, artists=limit, albums=limit, songs=limit)
    catalog_t = _quick_cached(q, limit)
    library, catalog = await asyncio.gather(library_t, catalog_t, return_exceptions=True)
    sources = {"library": "ok", "catalog": "ok"}
    if isinstance(library, BaseException):
        log.warning("quick search: library unavailable: %s", library)
        sources["library"] = "unavailable"
        library = {"artist": [], "album": [], "song": []}
    if isinstance(catalog, BaseException):
        if not isinstance(catalog, (httpx.HTTPError, ValueError)):
            raise catalog
        log.warning("quick search: catalogue unavailable: %s", catalog)
        sources["catalog"] = "unavailable"
        catalog = {"artists": [], "albums": [], "tracks": []}
    index = _LibraryIndex(lib_db.all_albums(), user_id)
    requests = req_db.active_by_release_group()
    results = (merge_albums(library["album"], catalog["albums"], index, requests, limit)
               + merge_artists(library["artist"], catalog["artists"], limit)
               + merge_tracks(library["song"], catalog["tracks"], index, requests, limit, req_db.active_tracks()))
    return {"query": q, "sources": sources, "results": results, "partial": True,
            "ranked": bool(catalog.get("ranked")), "best": lead(q, results, catalog.get("best"))}
