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
_FRESH_DAYS = 90
_SEEDS = 3
_SIMILAR = 6
_SONGS_EACH = 2
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


async def _for_you(albums: list[dict], limit: int) -> list[dict]:
    counts: dict[str, tuple[int, str]] = {}
    for a in albums:
        if a.get("artist"):
            n, _ = counts.get(normalise(a["artist"]), (0, a["artist"]))
            counts[normalise(a["artist"])] = (n + 1, a["artist"])
    # The most collected artists, rotating day by day among the top dozen so Home changes.
    top = sorted(counts.items(), key=lambda kv: -kv[1][0])[:12]
    if not top:
        return []
    start = date.today().toordinal() % len(top)
    seeds = [name for _, (_, name) in (top[start:] + top[:start])[:_SEEDS]]
    mbids = [m for m in await asyncio.gather(*(_artist_mbid(s) for s in seeds)) if m]
    similar = await asyncio.gather(*(listenbrainz.similar_artists(m, limit=15) for m in mbids))
    yours = set(counts)
    score: dict[str, float] = {}
    names: dict[str, str] = {}
    for lst in similar:
        for rank, a in enumerate(lst or []):
            if normalise(a["name"]) in yours:
                continue
            score[a["musicBrainzArtistId"]] = score.get(a["musicBrainzArtistId"], 0.0) + 1.0 / (rank + 1)
            names[a["musicBrainzArtistId"]] = a["name"]
    picks = sorted(score, key=lambda m: -score[m])[:_SIMILAR]
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


async def _build(user_id: Optional[int], limit: int) -> dict:
    albums = _your_albums(user_id)
    fresh_t = cached(_fresh_cache, "fresh", _fresh_releases)
    fresh, songs = await asyncio.gather(fresh_t, _for_you(albums, limit), return_exceptions=True)
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
    cat_new = new_from(albums, fresh, limit)
    # Already in the library under another release group (a reissue matched by name): left out.
    new = [h for h in merge.merge_albums([], cat_new, index, requests, limit)
           if (h.get("availability") or {}).get("state") != "in_library"]
    by_rg = {a["musicBrainzReleaseGroupId"]: a for a in cat_new}
    for hit in new:   # when it came out, for "2 days ago"
        rg = (hit.get("ids") or {}).get("musicBrainzReleaseGroupId")
        if rg in by_rg:
            hit["releaseDate"] = by_rg[rg]["releaseDate"]
    tracks = merge.merge_tracks([], songs, index, requests, limit, req_db.active_tracks())
    return {"newReleases": new, "forYou": tracks, "since": (date.today() - timedelta(days=_FRESH_DAYS)).isoformat()}


async def page(user_id: Optional[int], limit: int = 12) -> dict:
    """Home's discovery, cached per account for 3 h."""
    return await cached(_page_cache, (user_id, limit), lambda: _build(user_id, limit))
