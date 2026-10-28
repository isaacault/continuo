"""Discovery for the app's Home: what's new from your artists, and songs from artists like them.

- New from your artists: ListenBrainz's fresh releases (the last 90 days, site-wide, one call a
  few times a day) whose artist credit names an artist in the account's library, newest first;
  albums already in the library left out.
- For you: artists similar to the account's most collected ones (ListenBrainz's similar
  artists), not already in its library, and their most listened songs (needs LISTENBRAINZ_TOKEN).

Both in the search's hit shape, so Play now and Request work on them as in search. Library
albums carry no artist MBIDs, so artists are matched by name, and a seed's MBID comes from
Lidarr's artist search (no rate limit)."""
import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Optional

import httpx

from ..db import library as lib_db
from ..db import requests as req_db
from ..requests.normalise import normalise
from ..upstream import lidarr, listenbrainz
from . import merge
from .cache import TTLCache, cached

log = logging.getLogger("continuo")

_fresh_cache = TTLCache(6 * 3600.0, max_items=2)
_page_cache = TTLCache(3 * 3600.0, max_items=64)
_artist_art_cache = TTLCache(24 * 3600.0, max_items=512)
_slow_cache = TTLCache(24 * 3600.0, max_items=512)
_history_cache = TTLCache(6 * 3600.0, max_items=64)
# The last page built for each account, served while a fresh one is built (see page).
_last_page: dict = {}
_rebuilding: set = set()
_WARM_AT_ONCE = 4
_FRESH_DAYS = 90
_SEEDS = 3
_SIMILAR = 6
_SONGS_EACH = 2
# Explore: artists like yours, and each one's most listened album.
_EXPLORE_ARTISTS = 10
_EXPLORE_ALBUMS = 10
# Joiners in an artist credit: "Drake feat. Future", "FISHER & Laurel".
_REMIX = re.compile(r"\b(?:remix|rmx|edit|sped up|slowed)\b", re.IGNORECASE)
_CREDIT_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with|x|&|and)\s+|,\s*|\s*/\s*", re.IGNORECASE)


def credited(credit: Optional[str]) -> set[str]:
    """The normalised artist names in one artist credit, the whole credit among them."""
    if not credit:
        return set()
    return {normalise(credit)} | {normalise(p) for p in _CREDIT_SPLIT.split(credit) if p.strip()}


async def _fresh_releases() -> list[dict]:
    r = await listenbrainz._client("listenbrainz", listenbrainz._API_URL).get(
        "/explore/fresh-releases/", params={"days": _FRESH_DAYS, "past": "true", "future": "false"})
    r.raise_for_status()
    return (r.json().get("payload") or {}).get("releases") or []


def _your_albums(user_id: Optional[int]) -> list[dict]:
    index = merge._LibraryIndex(lib_db.all_albums(), user_id)
    return [a for a in index.by_id.values() if index.mine(a) is not None]


def new_from(albums: list[dict], fresh: list[dict], limit: int) -> list[dict]:
    """The [fresh] releases by artists of [albums] that aren't among them, newest first, as
    catalogue albums (Lidarr's normalised shape, which merge_albums takes)."""
    yours = {normalise(a["artist"]) for a in albums if a.get("artist")}
    have = {a.get("mbz_release_group_id") for a in albums} - {None}
    out, seen = [], set()
    for x in sorted(fresh, key=lambda x: x.get("release_date") or "", reverse=True):
        rg = x.get("release_group_mbid")
        if not rg or rg in have or rg in seen or not (credited(x.get("artist_credit_name")) & yours):
            continue
        seen.add(rg)
        day = x.get("release_date") or ""
        out.append({"kind": "album", "title": x.get("release_name"), "artist": x.get("artist_credit_name"),
                    "year": int(day[:4]) if day[:4].isdigit() else None,
                    "albumType": x.get("release_group_primary_type"),
                    "imageUrl": merge._caa(x.get("caa_release_mbid")),
                    "musicBrainzReleaseGroupId": rg, "musicBrainzArtistId": (x.get("artist_mbids") or [None])[0],
                    "releaseIds": [x["release_mbid"]] if x.get("release_mbid") else [], "owned": False,
                    "trackCount": None, "lidarrAlbumId": None, "releaseDate": day or None})
        if len(out) >= limit:
            break
    return out


async def _artist_mbid(name: str) -> Optional[str]:
    try:
        hits = await lidarr.search_artists(name)
    except (httpx.HTTPError, ValueError):
        return None
    want = normalise(name)
    return next((h["musicBrainzArtistId"] for h in hits if normalise(h.get("name")) == want), None)


async def _given(mbid: str) -> str:
    return mbid


async def _mbid_or_blank(name: str) -> str:
    return await _artist_mbid(name) or ""


async def _similar_or_blank(mbid: str) -> list:
    found = await listenbrainz.similar_artists(mbid, limit=15)
    return found if found is not None else []


async def listening_seeds(user_id: Optional[int]) -> list[tuple[str, Optional[str]]]:
    """The account's most played artists, from the listening history it scrobbles (Last.fm, else
    ListenBrainz, for the accounts linked for scrobbling), most played first; [] without one.
    Kept a few hours."""
    from ..db import scrobblers
    from ..upstream import scrobble
    links = scrobblers.all_for(user_id) if user_id is not None else {}

    async def look() -> list:
        fm = (links.get("lastfm") or {}).get("account")
        lb = (links.get("listenbrainz") or {}).get("account")
        found = await scrobble.lastfm_top_artists(fm) if fm else []
        if not found and lb:
            found = await scrobble.listenbrainz_top_artists(lb)
        return [list(x) for x in found]
    if not links:
        return []
    return [tuple(x) for x in await cached(_history_cache, user_id, look)]


async def _similar(albums: list[dict], user_id: Optional[int] = None) -> tuple[list[tuple[str, str]], set[str]]:
    """Artists like the account's most played ones (its listening history, else its most collected
    in the library) and not in its library, most alike first: ([(mbid, name)], the account's
    library artists, normalised)."""
    counts: dict[str, tuple[int, str]] = {}
    for a in albums:
        if a.get("artist"):
            n, _ = counts.get(normalise(a["artist"]), (0, a["artist"]))
            counts[normalise(a["artist"])] = (n + 1, a["artist"])
    # What's played most (else collected most), rotating day by day among the top dozen so the
    # page changes.
    played = await listening_seeds(user_id)
    top = [(name, mbid) for name, mbid in played[:12]] or \
        [(name, None) for _, (_, name) in sorted(counts.items(), key=lambda kv: -kv[1][0])[:12]]
    if not top:
        return [], set(counts)
    start = date.today().toordinal() % len(top)
    seeds = (top[start:] + top[:start])[:_SEEDS]
    # Seeds' IDs and their similar artists change slowly: kept a day.
    mbids = [m for m in await asyncio.gather(*(
        _given(mbid) if mbid else cached(_slow_cache, ("mbid", normalise(name)), lambda s=name: _mbid_or_blank(s))
        for name, mbid in seeds)) if m]
    similar = await asyncio.gather(*(cached(_slow_cache, ("similar", m), lambda m=m: _similar_or_blank(m)) for m in mbids))
    yours = set(counts)
    score: dict[str, float] = {}
    names: dict[str, str] = {}
    for lst in similar:
        for rank, a in enumerate(lst or []):
            if normalise(a["name"]) in yours:
                continue
            score[a["musicBrainzArtistId"]] = score.get(a["musicBrainzArtistId"], 0.0) + 1.0 / (rank + 1)
            names[a["musicBrainzArtistId"]] = a["name"]
    ranked = sorted(score, key=lambda m: -score[m])
    return [(m, names[m]) for m in ranked], yours


async def _for_you(picks: list[tuple[str, str]], yours: set[str], limit: int) -> list[dict]:
    names = dict(picks)
    picks = [m for m, _ in picks[:_SIMILAR]]
    songs = await asyncio.gather(*(listenbrainz.top_recordings(m, limit=_SONGS_EACH + 3) for m in picks))
    out, titles, per_artist = [], set(), {}
    for mbid, lst in zip(picks, songs, strict=False):
        for t in lst or []:
            title, credit = t.get("title") or "", t.get("artist") or names[mbid]
            lead = normalise(_CREDIT_SPLIT.split(credit)[0])
            # Each song once, the original rather than its remixes, at most two by one lead artist
            # (similar artists' top songs often feature the same one), none by an artist of yours.
            if not t.get("recordingMbid") or normalise(title) in titles or _REMIX.search(title) \
                    or per_artist.get(lead, 0) >= _SONGS_EACH or lead in yours:
                continue
            titles.add(normalise(title))
            per_artist[lead] = per_artist.get(lead, 0) + 1
            out.append({"kind": "track", "musicBrainzRecordingId": t["recordingMbid"], "title": title,
                        "artist": credit, "durationMs": t.get("durationMs"),
                        "album": {"title": t.get("releaseName"), "musicBrainzReleaseGroupId": None,
                                  "year": None, "albumType": None},
                        "imageUrl": merge._caa(t.get("releaseMbid"))})
    # Each artist's first song, then their second: one artist's songs don't sit together.
    firsts, seconds, seen = [], [], set()
    for t in out:
        lead = normalise(_CREDIT_SPLIT.split(t["artist"])[0])
        (seconds if lead in seen else firsts).append(t)
        seen.add(lead)
    return (firsts + seconds)[:limit]


async def _artist_picture(name: str) -> Optional[str]:
    """The artist's picture from Lidarr (fanart.tv), for a release with no cover yet; cached a day."""
    async def look() -> str:
        try:
            hits = await lidarr.search_artists(name)
        except (httpx.HTTPError, ValueError):
            return ""
        want = normalise(name)
        return next((h.get("imageUrl") or "" for h in hits if normalise(h.get("name")) == want), "")
    return await cached(_artist_art_cache, normalise(name), look) or None


async def _with_covers(albums: list[dict]) -> list[dict]:
    """Brand-new releases often have no cover anywhere yet (Cover Art Archive, Lidarr, Deezer
    checked): those show their artist's picture instead of a blank."""
    async def one(a: dict) -> dict:
        if a.get("imageUrl"):
            return a
        lead = _CREDIT_SPLIT.split(a.get("artist") or "")[0].strip()
        return {**a, "imageUrl": await _artist_picture(lead)} if lead else a
    return list(await asyncio.gather(*(one(a) for a in albums)))


async def _warm(sources: list[str]) -> None:
    """Fetch the page's pictures into the artwork cache before the phone asks for them: a cold
    Cover Art Archive image takes seconds (redirects to archive.org), a cached one 0.07 s."""
    from .. import artwork
    gate = asyncio.Semaphore(_WARM_AT_ONCE)

    async def one(src: str) -> None:
        try:
            url = artwork.sized(artwork.check_source(src), None)
        except artwork.Refused:
            return
        key = artwork.cache_key(url)
        if artwork.cache_get(key) is not None or artwork.known_missing(key):
            return
        async with gate:
            try:
                data, _kind = await artwork.fetch_image(url)
            except artwork.NotFound:
                artwork.remember_missing(key)
                return
            except (artwork.Refused, artwork.Unusable, httpx.HTTPError):
                return
            artwork.cache_put(key, data)
    await asyncio.gather(*(one(s) for s in dict.fromkeys(sources) if s))


_warming: set[asyncio.Task] = set()


async def _artist_tiles(picks: list[tuple[str, str]]) -> list[dict]:
    """Explore's artists: each with a picture where Deezer (with ranked search on) or Lidarr has one."""
    from .. import config
    from ..upstream import deezer

    async def one(mbid: str, name: str) -> dict:
        # Deezer first when ranked search is on (0.15 s, and it has nearly everyone's picture);
        # Lidarr's lookups are slower and taken more or less one at a time.
        picture = None
        if config.settings.search_deezer:
            try:
                picture = next((a.get("picture") for a in await deezer.search_artists(name, 3)
                                if normalise(a["name"]) == normalise(name) and a.get("picture")), None)
            except (deezer.Exhausted, httpx.HTTPError, ValueError):
                pass
        if picture is None:
            try:
                picture = next((h.get("imageUrl") for h in await lidarr.search_artists(name)
                                if h.get("musicBrainzArtistId") == mbid and h.get("imageUrl")), None)
            except (httpx.HTTPError, ValueError):
                pass
        return {"kind": "artist", "name": name, "musicBrainzArtistId": mbid, "imageUrl": picture, "lidarrArtistId": None}
    return list(await asyncio.gather(*(one(m, n) for m, n in picks)))


async def _album_tiles(picks: list[tuple[str, str]]) -> list[dict]:
    """Explore's albums: each similar artist's most listened album."""
    tops = await asyncio.gather(*(listenbrainz.top_release_groups(m, limit=1) for m, _ in picks))
    out = []
    for (mbid, name), lst in zip(picks, tops, strict=True):
        for a in lst or []:
            out.append({"kind": "album", "title": a["title"], "artist": name, "year": a["year"], "albumType": a["albumType"],
                        "imageUrl": merge._caa(a["caaReleaseMbid"]), "musicBrainzReleaseGroupId": a["musicBrainzReleaseGroupId"],
                        "musicBrainzArtistId": mbid, "releaseIds": [], "owned": False, "trackCount": None, "lidarrAlbumId": None})
    return out


async def _playlists(user_id: Optional[int]) -> list[dict]:
    """ListenBrainz's playlists for the account's linked ListenBrainz user (scrobbling)."""
    from ..db import scrobblers
    account = ((scrobblers.all_for(user_id) if user_id is not None else {}).get("listenbrainz") or {}).get("account")
    if not account:
        return []
    return await listenbrainz.created_for(account) or []


async def _build(user_id: Optional[int], limit: int) -> dict:
    albums = _your_albums(user_id)
    fresh_t = cached(_fresh_cache, "fresh", _fresh_releases)
    picks, yours = await _similar(albums, user_id)
    fresh, songs, artist_tiles, album_tiles, playlists = await asyncio.gather(
        fresh_t, _for_you(picks, yours, limit), _artist_tiles(picks[:_EXPLORE_ARTISTS]),
        _album_tiles(picks[:_EXPLORE_ALBUMS]), _playlists(user_id), return_exceptions=True)
    artist_tiles = [] if isinstance(artist_tiles, BaseException) else artist_tiles
    album_tiles = [] if isinstance(album_tiles, BaseException) else album_tiles
    playlists = [] if isinstance(playlists, BaseException) else playlists
    if isinstance(fresh, BaseException):
        if not isinstance(fresh, (httpx.HTTPError, ValueError)):
            raise fresh
        log.warning("discover: fresh releases failed: %s", fresh)
        fresh = []
    if isinstance(songs, BaseException):
        if not isinstance(songs, (httpx.HTTPError, ValueError)):
            raise songs
        log.warning("discover: for you failed: %s", songs)
        songs = []
    index = merge._LibraryIndex(lib_db.all_albums(), user_id)
    requests = req_db.active_by_release_group()
    cat_new = await _with_covers(new_from(albums, fresh, limit))
    task = asyncio.get_running_loop().create_task(_warm([a.get("imageUrl") for a in cat_new + artist_tiles + album_tiles]))
    _warming.add(task)
    task.add_done_callback(_warming.discard)
    # Already in the library under another release group (a reissue matched by name): left out.
    new = [h for h in merge.merge_albums([], cat_new, index, requests, limit)
           if (h.get("availability") or {}).get("state") != "in_library"]
    by_rg = {a["musicBrainzReleaseGroupId"]: a for a in cat_new}
    for hit in new:   # when it came out, for "2 days ago"
        rg = (hit.get("ids") or {}).get("musicBrainzReleaseGroupId")
        if rg in by_rg:
            hit["releaseDate"] = by_rg[rg]["releaseDate"]
    tracks = merge.merge_tracks([], songs, index, requests, limit, req_db.active_tracks())
    explore_albums = [h for h in merge.merge_albums([], album_tiles, index, requests, len(album_tiles))
                      if (h.get("availability") or {}).get("state") != "in_library"]
    return {"newReleases": new, "forYou": tracks, "artists": merge.merge_artists([], artist_tiles, len(artist_tiles)),
            "albums": explore_albums, "playlists": playlists,
            "since": (date.today() - timedelta(days=_FRESH_DAYS)).isoformat()}


async def playlist_page(user_id: Optional[int], playlist_id: str) -> Optional[dict]:
    """One of ListenBrainz's playlists as songs (search hits): Play now, Request."""
    p = await listenbrainz.playlist(playlist_id)
    if p is None:
        return None
    releases = await listenbrainz.recording_releases([t["recordingMbid"] for t in p["tracks"]])
    songs = []
    for t in p["tracks"]:
        rel = releases.get(t["recordingMbid"]) or {}
        songs.append({"kind": "track", "musicBrainzRecordingId": t["recordingMbid"], "title": t["title"],
                      "artist": t.get("artist"), "durationMs": t.get("durationMs"),
                      "album": {"title": rel.get("name") or t.get("releaseName"), "musicBrainzReleaseGroupId": rel.get("releaseGroupMbid"),
                                "year": rel.get("year"), "albumType": None},
                      "imageUrl": merge._caa(t.get("caaReleaseMbid") or rel.get("caaReleaseMbid"))})
    index = merge._LibraryIndex(lib_db.all_albums(), user_id)
    tracks = merge.merge_tracks([], songs, index, req_db.active_by_release_group(), len(songs), req_db.active_tracks())
    return {"id": playlist_id, "title": p["title"], "description": p["description"], "tracks": tracks}


async def page(user_id: Optional[int], limit: int = 12) -> dict:
    """Home's and Explore's discovery, cached per account for 3 h. Past that the last page is
    served at once and a fresh one built behind it: building takes seconds (similar artists,
    pictures, top albums), and only the very first ask waits for it."""
    key = (user_id, limit)
    hit = _page_cache.get(key)
    if hit is not None:
        return hit
    if key in _last_page:
        if key not in _rebuilding:
            _rebuilding.add(key)
            task = asyncio.get_running_loop().create_task(_rebuild(key))
            _warming.add(task)
            task.add_done_callback(_warming.discard)
        return _last_page[key]
    return await _rebuild(key)


async def _rebuild(key) -> dict:
    try:
        value = await _build(*key)
        _page_cache.put(key, value)
        _last_page[key] = value
        return value
    finally:
        _rebuilding.discard(key)
