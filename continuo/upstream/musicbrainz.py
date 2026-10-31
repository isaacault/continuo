# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""MusicBrainz: track search and tracklists. One shared client with the required User-Agent,
a ~1 req/s pacer (MusicBrainz's rate limit), and caches (lidseeker opened a fresh client per call
and cached nothing)."""
import asyncio
import time
from typing import Optional

import httpx

from .. import config
from ..requests.normalise import track_key
from . import http
from .lidarr import _year, coverart_url

_MB_URL = "https://musicbrainz.org/ws/2"
_RG_TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}
_MIN_INTERVAL = 1.05
_SEARCH_TTL = 600.0

_pace_lock = asyncio.Lock()
_last_call = 0.0
_search_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_tracklist_cache: dict[str, list[dict]] = {}
_TRACKLIST_CACHE_MAX = 512


def _client() -> httpx.AsyncClient:
    return http.client("musicbrainz", base_url=_MB_URL, timeout=20.0,
                       headers={"User-Agent": config.settings.musicbrainz_user_agent,
                                "Accept": "application/json"})


class Unavailable(Exception):
    """MusicBrainz didn't answer (it sheds load with 503s when busy), as opposed to not knowing."""


# Waits before each retry of a 503/429/5xx; MusicBrainz's own Retry-After wins when it says more.
_RETRY_WAITS = (1.0, 3.0)


# How long an answer on disk is fresh: a release's tracklist practically never changes; names
# and release lists now and then; searches find new things. Stale answers are still used when
# MusicBrainz can't be asked.
_TTL_DAYS = {"release": 30, "search": 1}
_TTL_DEFAULT_DAYS = 7
_CACHE_MAX_BYTES = 200 * 1024 * 1024
_writes = 0


def _cache_key(path: str, params: dict) -> str:
    return path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


def _ttl_s(path: str, params: dict) -> float:
    kind = "search" if "query" in params else path.strip("/").split("/")[0]
    return _TTL_DAYS.get(kind, _TTL_DEFAULT_DAYS) * 86400.0


async def _get(path: str, params: dict, retries: int = len(_RETRY_WAITS)) -> dict:
    """A MusicBrainz answer: from the disk cache while fresh, else asked (and kept). When
    MusicBrainz can't be asked (still busy after the retries, or unreachable), a stale cached
    answer is used; without one the error is raised. A 404 is never cached or covered."""
    from ..db import musicbrainz_cache as cache_db
    from ..db.conn import seconds_since
    global _writes
    key = _cache_key(path, params)
    hit = cache_db.get(key)
    if hit and (seconds_since(hit[1]) or 0) < _ttl_s(path, params):
        return hit[0]
    try:
        body = await _ask(path, params, retries)
    except httpx.HTTPStatusError as e:
        if hit and e.response.status_code not in (400, 404):
            return hit[0]
        raise
    except httpx.HTTPError:
        if hit:
            return hit[0]
        raise
    cache_db.put(key, body)
    _writes += 1
    if _writes % 100 == 0:
        cache_db.prune(_CACHE_MAX_BYTES)
    return body


async def _ask(path: str, params: dict, retries: int) -> dict:
    """One paced MusicBrainz call. A busy MusicBrainz (503 "global" rate limit, 429, other 5xx)
    is retried; httpx.HTTPStatusError when it still fails, or for a 404."""
    global _last_call
    for attempt in range(retries + 1):
        async with _pace_lock:
            wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await _client().get(path, params={**params, "fmt": "json"})
            finally:
                _last_call = time.monotonic()
        if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
            after = r.headers.get("retry-after", "")
            await asyncio.sleep(min(10.0, max(_RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)],
                                              float(after) if after.replace(".", "", 1).isdigit() else 0)))
            continue
        r.raise_for_status()
        return r.json()
    raise AssertionError("unreachable")


async def _lookup(path: str, params: dict) -> Optional[dict]:
    """A lookup by MBID: None when MusicBrainz doesn't know it (404), Unavailable when it
    couldn't be asked."""
    try:
        return await _get(path, params)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 404):
            return None
        raise Unavailable(f"MusicBrainz answered {e.response.status_code}") from e
    except (httpx.HTTPError, ValueError) as e:
        raise Unavailable(str(e) or e.__class__.__name__) from e


def pick_release_group(rec: dict) -> Optional[dict]:
    """Best parent release-group for a recording: an official Album/EP without
    compilation/live secondary types, earliest first."""
    best, best_key = None, None
    for rel in rec.get("releases", []):
        rg = rel.get("release-group") or {}
        rg_id = rg.get("id")
        if not rg_id:
            continue
        primary = rg.get("primary-type") or ""
        rank = _RG_TYPE_RANK.get(primary, 9) + (5 if rg.get("secondary-types") else 0)
        rg_date = rel.get("date") or rg.get("first-release-date") or "9999"
        key = (rank, rg_date)
        if best_key is None or key < best_key:
            best_key, best = key, {"id": rg_id, "title": rg.get("title") or rel.get("title"),
                                   "primary": primary, "date": rg_date}
    return best


async def search_tracks(term: str, limit: int = 25) -> list[dict]:
    """Recordings matching `term`, each pointing at the album that would fulfil a request.
    Raises httpx.HTTPError on failure (the caller reports the catalogue as degraded). One quick
    retry only: someone is waiting on the search."""
    key = (term.strip().casefold(), limit)
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < _SEARCH_TTL:
        return hit[1]
    data = await _get("/recording", {"query": term, "limit": limit}, retries=1)
    out: list[dict] = []
    seen: set[str] = set()
    for rec in data.get("recordings", []):
        rg = pick_release_group(rec)
        if not rg:
            continue
        credit = rec.get("artist-credit") or []
        artist = credit[0].get("name") if credit else None
        dedupe = track_key(artist, rec.get("title"))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append({
            "kind": "track",
            "musicBrainzRecordingId": rec.get("id"),
            "title": rec.get("title"),
            "artist": artist,
            "durationMs": rec.get("length"),
            "album": {
                "title": rg.get("title"),
                "musicBrainzReleaseGroupId": rg["id"],
                "year": _year(rg.get("date")),
                "albumType": rg.get("primary"),
            },
            "imageUrl": coverart_url(rg["id"]),
        })
    if len(_search_cache) > 256:
        _search_cache.clear()
    _search_cache[key] = (time.monotonic(), out)
    return out


def searched_recording(recording_mbid: str) -> Optional[dict]:
    """A recording from a recent search (title, artist, album), without asking MusicBrainz:
    what a song request shows while MusicBrainz is too busy to look it up."""
    for _ts, hits in list(_search_cache.values()):
        for h in hits:
            if h.get("musicBrainzRecordingId") == recording_mbid:
                return h
    return None


def _track_count(rel: dict) -> int:
    return sum(len(m.get("tracks") or []) for m in rel.get("media") or [])


def _pick_standard(releases: list[dict]) -> Optional[str]:
    """The group's standard edition: the official release with the fewest songs (a deluxe edition's
    bonus tracks don't make the library's album partial), leaving out any under half the largest
    (a promo, a sampler); the earliest of equals. None without official releases with tracks."""
    official = [r for r in releases if r.get("status") == "Official" and _track_count(r) > 0]
    if not official:
        return None
    most = max(_track_count(r) for r in official)
    full = [r for r in official if _track_count(r) * 2 >= most]
    return min(full, key=lambda r: (_track_count(r), r.get("date") or "9999")).get("id")


def _pick_release(releases: list[dict]) -> Optional[str]:
    if not releases:
        return None
    return min(releases, key=lambda rel: (0 if rel.get("status") == "Official" else 1,
                                         rel.get("date") or "9999")).get("id")


async def album_tracks(release_group_mbid: str, release_mbid: Optional[str] = None,
                       strict: bool = False, standard: bool = False) -> list[dict]:
    """Tracklist of one canonical official release of the group (or the given release).
    A release's tracklist never changes, so it's cached for the process life (bounded).
    [] when MusicBrainz fails, or with [strict] raises Unavailable.

    For a group, one call: the group's official releases with their tracks (a browse), the
    canonical one picked from them; MusicBrainz is paced to a call a second, so the two-step
    group-then-release lookup cost an album page a second. Without official releases, the
    two-step one is the fallback.

    [standard]: the standard edition's instead (see _pick_standard): what a library album is
    measured against, for whether it's complete and what's missing from it."""
    cache_key = release_mbid or (release_group_mbid + (":standard" if standard else ""))
    if cache_key in _tracklist_cache:
        return _tracklist_cache[cache_key]
    try:
        rel = None
        if not release_mbid:
            browse = await _get("/release", {"release-group": release_group_mbid, "inc": "recordings",
                                             "status": "official", "limit": 25})
            releases = browse.get("releases") or []
            pick = (_pick_standard(releases) if standard else None) or _pick_release(releases)
            rel = next((r for r in releases if r.get("id") == pick), None)
            if rel is None:
                rg = await _get(f"/release-group/{release_group_mbid}", {"inc": "releases"})
                release_mbid = _pick_release(rg.get("releases", []))
                if not release_mbid:
                    return []
        if rel is None:
            rel = await _get(f"/release/{release_mbid}", {"inc": "recordings"})
    except (httpx.HTTPError, ValueError) as e:
        if strict:
            raise Unavailable(str(e) or e.__class__.__name__) from e
        return []
    out: list[dict] = []
    for medium in rel.get("media", []):
        disc = medium.get("position") or 1
        for t in medium.get("tracks", []):
            recording = t.get("recording") or {}
            title = t.get("title") or recording.get("title")
            if not title:
                continue
            length = t.get("length") or recording.get("length")
            out.append({
                "position": t.get("position") or len(out) + 1,
                "title": title,
                "durationMs": int(length) if length else None,
                "discNumber": disc,
                "musicBrainzRecordingId": recording.get("id"),
            })
    if len(_tracklist_cache) >= _TRACKLIST_CACHE_MAX:
        del _tracklist_cache[next(iter(_tracklist_cache))]
    _tracklist_cache[cache_key] = out
    return out


async def release_group_for_title(artist_mbid: str, title: str) -> Optional[str]:
    """The release group of this artist's release called [title], via MusicBrainz's release
    search, which also knows translated and romanised release titles ("Windswept Adan" is a
    release of アダンの風). None when nothing scores high enough."""
    q = title.replace("\\", " ").replace('"', " ").strip()
    if not q:
        return None
    try:
        data = await _get("/release", {"query": f'release:"{q}" AND arid:{artist_mbid}', "limit": 5})
    except (httpx.HTTPError, ValueError):
        return None
    for rel in data.get("releases") or []:
        rg = (rel.get("release-group") or {}).get("id")
        if rg and int(rel.get("score") or 0) >= 90:
            return rg
    return None


async def release_group_by_names(artist: str, title: str) -> Optional[str]:
    """The release group of [artist]'s release called [title], by name alone (no artist MBID: an
    untagged library album's): MusicBrainz's release search, a near-certain score, and the credit's
    name the library's. None when nothing is that sure."""
    from ..requests.normalise import normalise
    a = (artist or "").replace("\\", " ").replace('"', " ").strip()
    t = (title or "").replace("\\", " ").replace('"', " ").strip()
    if not a or not t:
        return None
    try:
        data = await _get("/release", {"query": f'release:"{t}" AND artist:"{a}"', "limit": 5})
    except (httpx.HTTPError, ValueError):
        return None
    want = normalise(a)
    for rel in data.get("releases") or []:
        rg = (rel.get("release-group") or {}).get("id")
        credited, _ = _credit(rel.get("artist-credit") or [])
        if rg and int(rel.get("score") or 0) >= 95 and credited and want in (normalise(credited), normalise(credited.split(" feat")[0])):
            return rg
    return None


def _credit(credits: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """(the credited name as printed, the first artist's MBID)."""
    name = "".join((c.get("name") or (c.get("artist") or {}).get("name") or "") + (c.get("joinphrase") or "")
                   for c in credits or []).strip()
    first = (credits or [{}])[0].get("artist") or {}
    return name or None, first.get("id")


async def recording(recording_mbid: str) -> Optional[dict]:
    """{title, artist, artistId, durationMs} for a recording; None when MusicBrainz doesn't
    know it. Raises Unavailable when MusicBrainz couldn't be asked."""
    r = await _lookup(f"/recording/{recording_mbid}", {"inc": "artist-credits"})
    if r is None:
        return None
    artist, artist_id = _credit(r.get("artist-credit"))
    return {"title": r.get("title"), "artist": artist, "artistId": artist_id,
            "durationMs": int(r["length"]) if r.get("length") else None}


async def album_info(release_group_mbid: str) -> Optional[dict]:
    """What filing a song into this album needs: {title, artist, artistId, date, year,
    releaseId, imageUrl, tracks} (tracks as album_tracks, from the same release). None when
    MusicBrainz doesn't know the album; raises Unavailable when it couldn't be asked."""
    rg = await _lookup(f"/release-group/{release_group_mbid}", {"inc": "releases+artist-credits"})
    if rg is None:
        return None
    release_id = _pick_release(rg.get("releases", []))
    artist, artist_id = _credit(rg.get("artist-credit"))
    date = rg.get("first-release-date") or None
    return {"title": rg.get("title"), "artist": artist, "artistId": artist_id, "date": date,
            "year": _year(date), "releaseId": release_id, "imageUrl": coverart_url(release_group_mbid),
            "tracks": await album_tracks(release_group_mbid, release_id, strict=True) if release_id else []}


_artist_cache: dict[str, tuple[float, Optional[dict]]] = {}
_ARTIST_TTL = 24 * 3600.0


async def artist_names(artist_mbid: str) -> Optional[dict]:
    """{name, sortName, aliases: [{name, locale, primary}]} for a MusicBrainz artist; None when
    MusicBrainz didn't answer. Cached a day: names hardly change."""
    hit = _artist_cache.get(artist_mbid)
    if hit and time.monotonic() - hit[0] < _ARTIST_TTL:
        return hit[1]
    try:
        a = await _get(f"/artist/{artist_mbid}", {"inc": "aliases"})
    except (httpx.HTTPError, ValueError):
        return None
    out = {"name": a.get("name"), "sortName": a.get("sort-name"),
           "aliases": [{"name": al.get("name"), "locale": al.get("locale"), "primary": bool(al.get("primary"))}
                       for al in a.get("aliases") or [] if al.get("name")]}
    _artist_cache[artist_mbid] = (time.monotonic(), out)
    return out
