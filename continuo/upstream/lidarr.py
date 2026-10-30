# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""Async client over the Lidarr v1 endpoints continuo uses.

Lidarr is artist-centric: to request an album we make sure its artist exists (added with nothing
monitored and monitorNewItems=none), then monitor exactly that album. Nothing here ever monitors
a discography: `request_artist` was deliberately not ported (policy: nothing follows artists).
The request flow itself lives in continuo.requests.orchestrate; this module is the API surface.
"""
import asyncio
import logging
import time
from typing import Optional

import httpx

from .. import artwork, config
from . import http

log = logging.getLogger("continuo")

# Cover-type preference per result kind.
_IMAGE_PREF = {
    "album": ("cover", "disc"),
    "artist": ("poster", "fanart", "cover", "banner"),
}

_METADATA_URL = "https://api.lidarr.audio/api/v0.4"
_ARTIST_BROWSE_TYPES = {"Album", "EP", "Single"}


def _client() -> httpx.AsyncClient:
    s = config.settings
    return http.client("lidarr", base_url=f"{s.lidarr_url}/api/v1",
                       headers={"X-Api-Key": s.lidarr_api_key}, timeout=30.0)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
def _best_image(images: list, kind: str) -> Optional[str]:
    """An absolute image URL a client can load directly. Album art comes back as absolute
    images.lidarr.audio URLs; artist art only as API-key-less local paths → None."""
    if not images:
        return None
    by_type = {img.get("coverType"): img for img in images}
    chosen = None
    for pref in _IMAGE_PREF.get(kind, ()):
        if pref in by_type:
            chosen = by_type[pref]
            break
    if chosen is None:
        chosen = images[0]
    remote = chosen.get("remoteUrl") or ""
    return remote if remote.startswith("http") else None


def _year(date_str: Optional[str]) -> Optional[int]:
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        y = int(date_str[:4])
        return y if y > 0 else None
    return None


def coverart_url(release_group_mbid: Optional[str]) -> Optional[str]:
    if not release_group_mbid:
        return None
    return f"https://coverartarchive.org/release-group/{release_group_mbid}/front-500"


def _album_image(item: dict) -> Optional[str]:
    """Lidarr's album image when continuo can proxy it (/v1/artwork), else Cover Art Archive."""
    img = _best_image(item.get("images", []), "album")
    return img if artwork.proxyable(img) else coverart_url(item.get("foreignAlbumId"))


def normalize_artist(item: dict) -> dict:
    return {
        "kind": "artist",
        "musicBrainzArtistId": item.get("foreignArtistId"),
        "name": item.get("artistName"),
        "imageUrl": _best_image(item.get("images", []), "artist"),
        "lidarrArtistId": item.get("id") or None,
    }


def normalize_album(item: dict) -> dict:
    """The catalogue view of one album. `owned` means Lidarr has its files (fix/88: a bare id
    only means Lidarr *knows* the album, true for a library artist's whole discography)."""
    artist = item.get("artist", {}) or {}
    stats = item.get("statistics") or {}
    releases = item.get("releases") or []
    track_counts = [r.get("trackCount") for r in releases if r.get("trackCount")]
    return {
        "kind": "album",
        "musicBrainzReleaseGroupId": item.get("foreignAlbumId"),
        "title": item.get("title"),
        "artist": artist.get("artistName"),
        "musicBrainzArtistId": artist.get("foreignArtistId"),
        "year": _year(item.get("releaseDate")),
        "albumType": item.get("albumType"),
        "imageUrl": _album_image(item),
        "lidarrAlbumId": item.get("id") or None,
        "releaseIds": [r.get("foreignReleaseId") for r in releases if r.get("foreignReleaseId")],
        "trackFileCount": stats.get("trackFileCount") or 0,
        "trackCount": stats.get("trackCount") or (min(track_counts) if track_counts else None),
        "owned": (stats.get("trackFileCount") or 0) > 0,
    }


# --------------------------------------------------------------------------
# Search / lookup
# --------------------------------------------------------------------------
async def search_albums(term: str) -> list[dict]:
    c = _client()
    r = await c.get("/album/lookup", params={"term": term})
    r.raise_for_status()
    items = r.json()
    await _attach_album_statistics(c, items)
    return [normalize_album(x) for x in items]


async def search_artists(term: str) -> list[dict]:
    c = _client()
    r = await c.get("/artist/lookup", params={"term": term})
    r.raise_for_status()
    return [normalize_artist(x) for x in r.json()]


async def _attach_album_statistics(c: httpx.AsyncClient, items: list[dict]) -> None:
    """/album/lookup carries an id for every album Lidarr knows but never statistics. Fetch
    those ids' library rows in one call so owned can be told from merely known (fix/88). On
    failure the rows stay stat-less and read as not owned."""
    ids = [x["id"] for x in items if x.get("id") and x.get("statistics") is None]
    if not ids:
        return
    try:
        r = await c.get("/album", params=[("albumIds", i) for i in ids])
        r.raise_for_status()
        stats = {a.get("id"): a.get("statistics") for a in r.json()}
    except (httpx.HTTPError, ValueError):
        return
    for x in items:
        if x.get("id") in stats:
            x["statistics"] = stats[x["id"]]


async def lookup_album_by_id(foreign_album_id: str) -> Optional[dict]:
    r = await _client().get("/album/lookup", params={"term": f"lidarr:{foreign_album_id}"})
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None


async def lookup_artist_by_id(foreign_artist_id: str) -> Optional[dict]:
    r = await _client().get("/artist/lookup", params={"term": f"lidarr:{foreign_artist_id}"})
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None


async def artist_albums(foreign_artist_id: str) -> list[dict]:
    """An artist's albums: from the library when the artist is in Lidarr, else from Lidarr's
    metadata server (Lidarr's own lookup can't list a non-library artist's discography)."""
    existing = await find_library_artist(foreign_artist_id)
    if existing:
        return [normalize_album(x) for x in await albums_for_artist_id(existing["id"])]
    return await _metadata_artist_albums(foreign_artist_id)


async def metadata_artist(foreign_artist_id: str) -> Optional[dict]:
    """Lidarr's metadata server's artist (name, overview, images, Albums), library or not; None
    when it's unreachable or doesn't know the artist."""
    try:
        c = http.client("lidarr-metadata", timeout=20.0)
        r = await c.get(f"{_METADATA_URL}/artist/{foreign_artist_id}")
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def metadata_image(data: dict) -> Optional[str]:
    """The artist picture from a metadata-server artist (absolute images.lidarr.audio URLs,
    unlike the Lidarr API's local paths): poster first, then fanart."""
    by_type = {(i.get("CoverType") or i.get("coverType") or "").lower(): i for i in data.get("images") or []}
    for pref in ("poster", "fanart", "cover", "banner"):
        url = (by_type.get(pref) or {}).get("Url") or (by_type.get(pref) or {}).get("url")
        if url and url.startswith("http"):
            return url
    return None


async def _metadata_artist_albums(foreign_artist_id: str) -> list[dict]:
    data = await metadata_artist(foreign_artist_id)
    if data is None:
        return []
    name = data.get("artistname") or data.get("artistName")
    out: list[dict] = []
    for a in data.get("Albums") or data.get("albums") or []:
        atype = a.get("Type") or a.get("type")
        statuses = a.get("ReleaseStatuses") or a.get("releaseStatuses") or []
        if atype not in _ARTIST_BROWSE_TYPES or "Official" not in statuses:
            continue
        rg = a.get("Id") or a.get("id")
        out.append({
            "kind": "album",
            "musicBrainzReleaseGroupId": rg,
            "title": a.get("Title") or a.get("title"),
            "artist": name,
            "musicBrainzArtistId": foreign_artist_id,
            "year": _year(a.get("ReleaseDate") or a.get("releaseDate")),
            "albumType": atype,
            "imageUrl": coverart_url(rg),
            "lidarrAlbumId": None,
            "releaseIds": [],
            "trackFileCount": 0,
            "trackCount": None,
            "owned": False,
        })
    out.sort(key=lambda x: x["year"] or 0, reverse=True)
    return out


# --------------------------------------------------------------------------
# Library state + mutations
# --------------------------------------------------------------------------
async def find_library_artist(foreign_artist_id: Optional[str]) -> Optional[dict]:
    """The library artist with this MBID. Uses `GET /artist?mbId=` instead of lidseeker's
    list-every-artist scan."""
    if not foreign_artist_id:
        return None
    r = await _client().get("/artist", params={"mbId": foreign_artist_id})
    r.raise_for_status()
    for a in r.json():
        if a.get("foreignArtistId") == foreign_artist_id:
            return a
    return None


async def add_artist(lookup_artist: dict) -> dict:
    """Add an artist with NOTHING monitored and monitorNewItems=none. Left unset,
    monitorNewItems defaults to "all" and the RefreshArtist after an album request then
    re-monitors the whole discography."""
    s = config.settings
    payload = dict(lookup_artist)
    payload.update({
        "qualityProfileId": s.quality_profile_id,
        "metadataProfileId": s.metadata_profile_id,
        "rootFolderPath": s.root_folder_path,
        "monitored": True,
        "monitorNewItems": "none",
        "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
    })
    r = await _client().post("/artist", json=payload)
    r.raise_for_status()
    return r.json()


async def albums_for_artist_id(artist_id: int) -> list[dict]:
    """Every album for an artist, paginating (the default page is ~15 albums). Lidarr 3 ignores
    page/pageSize here and answers every page with the whole list, so a page that adds no album not
    already seen ends it: an artist with 100+ albums (Drake: 106) otherwise looped forever, a
    megabyte a page, until continuo ran out of memory and restarted (over a thousand times)."""
    all_albums: list[dict] = []
    seen: set = set()
    page, page_size = 1, 100
    while True:
        r = await _client().get("/album", params={
            "artistId": artist_id, "page": page, "pageSize": page_size,
        })
        r.raise_for_status()
        page_data = r.json()
        new = [a for a in page_data if a.get("id") not in seen]
        if not new:
            break
        seen.update(a.get("id") for a in new)
        all_albums.extend(new)
        if len(page_data) < page_size:
            break
        page += 1
    return all_albums


async def wait_for_album(artist_id: int, foreign_album_id: str,
                         tries: int = 20, delay: float = 1.5) -> Optional[dict]:
    """After adding an artist Lidarr imports its albums asynchronously."""
    for _ in range(tries):
        for alb in await albums_for_artist_id(artist_id):
            if alb.get("foreignAlbumId") == foreign_album_id:
                return alb
        await asyncio.sleep(delay)
    return None


async def find_album_by_foreign_id(foreign_album_id: Optional[str]) -> Optional[dict]:
    """The library album with this release-group MBID, if Lidarr has it."""
    if not foreign_album_id:
        return None
    r = await _client().get("/album", params={"foreignAlbumId": foreign_album_id})
    r.raise_for_status()
    for a in r.json():
        if a.get("foreignAlbumId") == foreign_album_id:
            return a
    return None


async def imported_paths(artist_id: int) -> list[tuple[str, str]]:
    """(droppedPath, importedPath) of every file Lidarr imported for this artist: where it came
    from (a downloads folder, or a manual import of the user's own files) and where it went."""
    r = await _client().get("/history/artist", params={"artistId": artist_id, "eventType": "trackFileImported"})
    r.raise_for_status()
    out = []
    for e in r.json() or []:
        d = e.get("data") or {}
        if d.get("importedPath"):
            out.append((d.get("droppedPath") or "", d["importedPath"]))
    return out


async def set_album_monitored(album_ids: list[int], monitored: bool = True) -> None:
    r = await _client().put("/album/monitor", json={"albumIds": album_ids, "monitored": monitored})
    r.raise_for_status()
    for aid in album_ids:
        _album_cache.pop(aid, None)


async def get_artist(artist_id: int) -> dict:
    r = await _client().get(f"/artist/{artist_id}")
    r.raise_for_status()
    return r.json()


async def set_artist_monitored(artist_id: int, monitored: bool = True) -> None:
    """Flip the artist's top-level `monitored` flag (Lidarr only puts albums of monitored artists
    in wanted/missing, the pool Soularr reads) and pin monitorNewItems=none, WITHOUT changing
    which albums are monitored: a plain PUT with no addOptions."""
    artist = await get_artist(artist_id)
    if artist.get("monitored") == monitored and artist.get("monitorNewItems") == "none":
        return
    artist["monitored"] = monitored
    artist["monitorNewItems"] = "none"
    r = await _client().put(f"/artist/{artist_id}", json=artist)
    r.raise_for_status()


async def album_search(album_ids: list[int]) -> None:
    r = await _client().post("/command", json={"name": "AlbumSearch", "albumIds": album_ids})
    r.raise_for_status()


async def run_command(name: str, wait: bool = True, timeout: float = 60.0, **fields) -> None:
    """Fire a Lidarr command and (by default) block until it finishes: an unawaited
    RefreshArtist can complete after we monitored an album and silently revert it."""
    c = _client()
    r = await c.post("/command", json={"name": name, **fields})
    r.raise_for_status()
    cmd_id = r.json().get("id")
    if not wait or not cmd_id:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        rr = await c.get(f"/command/{cmd_id}")
        rr.raise_for_status()
        if rr.json().get("status") in ("completed", "failed", "aborted"):
            return


async def refresh_artist(artist_id: int) -> None:
    await run_command("RefreshArtist", artistId=artist_id)


# Short-lived cache for /album/{id}: the status loop, the request list and the pipeline read the
# same album within seconds. (Lidseeker defined _get_album twice; this is the cached one.)
_ALBUM_TTL = 3.0
_ALBUM_CACHE_MAX = 512
_album_cache: dict[int, tuple[float, dict]] = {}


async def get_album(album_id: int, fresh: bool = False) -> dict:
    now = time.monotonic()
    hit = _album_cache.get(album_id)
    if hit and not fresh and now - hit[0] < _ALBUM_TTL:
        return hit[1]
    r = await _client().get(f"/album/{album_id}")
    r.raise_for_status()
    data = r.json()
    if len(_album_cache) >= _ALBUM_CACHE_MAX:
        for k in [k for k, (ts, _) in _album_cache.items() if now - ts >= _ALBUM_TTL]:
            del _album_cache[k]
        while len(_album_cache) >= _ALBUM_CACHE_MAX:
            del _album_cache[next(iter(_album_cache))]
    _album_cache[album_id] = (now, data)
    return data


async def album_stats(album_id: int) -> dict:
    return (await get_album(album_id)).get("statistics", {}) or {}


def monitored_release_id(album: dict) -> Optional[str]:
    for rel in album.get("releases") or []:
        if rel.get("monitored"):
            return rel.get("foreignReleaseId")
    return None


async def monitor_album(artist_id: int, album_id: int) -> bool:
    """Monitor exactly one album and build its track list without touching any other album.

    The trap: Lidarr only builds a track list while the album is monitored, but with
    monitorNewItems=none a RefreshArtist un-monitors a "new" album. So: monitor → blocking
    RefreshArtist → re-monitor (now no longer "new", it sticks) → verify."""
    await set_album_monitored([album_id], True)
    await refresh_artist(artist_id)
    await set_album_monitored([album_id], True)
    album = await get_album(album_id, fresh=True)
    tracks = (album.get("statistics") or {}).get("trackCount") or 0
    return bool(album.get("monitored") and tracks > 0)


async def ensure_album_monitored(artist_id: int, album_id: int) -> bool:
    """Re-assert monitoring for one requested album if Lidarr's scheduled "refresh all" reverted
    it, and keep the artist at monitorNewItems=none. Cheap no-op when already right."""
    try:
        album = await get_album(album_id, fresh=True)
    except httpx.HTTPError:
        return False
    stats = album.get("statistics") or {}
    if (stats.get("trackFileCount") or 0) > 0:
        return True
    if album.get("monitored") and (stats.get("trackCount") or 0) > 0:
        try:
            artist = await get_artist(artist_id)
        except httpx.HTTPError:
            return False
        if artist.get("monitored") and artist.get("monitorNewItems") == "none":
            return True
        if artist.get("monitored"):
            # Album fine; only the artist drifted to following new releases. Pin it back.
            await set_artist_monitored(artist_id, True)
            return True
    await set_artist_monitored(artist_id, True)
    return await monitor_album(artist_id, album_id)


async def track_files(album_id: int) -> list[dict]:
    r = await _client().get("/trackfile", params={"albumId": album_id})
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------
def _queue_record(rec: dict) -> dict:
    messages = []
    for m in rec.get("statusMessages") or []:
        messages.extend(m.get("messages") or [])
        if m.get("title") and not m.get("messages"):
            messages.append(m["title"])
    return {
        "id": rec.get("id"),
        "albumId": rec.get("albumId"),
        "protocol": rec.get("protocol"),                       # torrent | usenet | unknown
        "downloadClient": rec.get("downloadClient"),
        "status": rec.get("status"),
        "trackedDownloadStatus": rec.get("trackedDownloadStatus"),   # ok | warning | error
        "trackedDownloadState": rec.get("trackedDownloadState"),     # downloading | importPending | importing | …
        "size": rec.get("size"),
        "sizeLeft": rec.get("sizeleft"),
        "timeLeft": rec.get("timeleft"),
        "title": rec.get("title"),
        "messages": messages,
        "errorMessage": rec.get("errorMessage"),
    }


async def queue_index() -> dict[int, dict]:
    """albumId -> queue record for everything in Lidarr's queue, fetched ONCE per batch.
    Returns {} on upstream error (callers degrade to a coarser stage)."""
    try:
        out: dict[int, dict] = {}
        page = 1
        while True:
            r = await _client().get("/queue", params={"page": page, "pageSize": 200,
                                                      "includeUnknownArtistItems": "false"})
            r.raise_for_status()
            data = r.json()
            for rec in data.get("records", []):
                q = _queue_record(rec)
                if q["albumId"] is not None:
                    out[q["albumId"]] = q
            if page * 200 >= (data.get("totalRecords") or 0):
                return out
            page += 1
    except (httpx.HTTPError, ValueError):
        return {}


async def remove_queue_item(queue_id: int, blocklist: bool = True) -> None:
    """Drop a download: remove it from the client and (by default) blocklist the release, so the
    album stays wanted and Soularr (which skips albums already in the queue) can pick it up. A
    race's slow loser isn't blocklisted: it wasn't bad, only slower."""
    r = await _client().delete(f"/queue/{queue_id}", params={
        "removeFromClient": "true", "blocklist": "true" if blocklist else "false",
    })
    r.raise_for_status()


async def album_tracks(album_id: int) -> list[dict]:
    """The album's tracks as Lidarr knows them (title, trackNumber, mediumNumber)."""
    r = await _client().get("/track", params={"albumId": album_id})
    r.raise_for_status()
    return r.json()


async def import_folder(path: str) -> None:
    """Have Lidarr import a downloaded folder (Lidarr's own path), as Soularr does."""
    r = await _client().post("/command", json={"name": "DownloadedAlbumsScan", "path": path})
    r.raise_for_status()


# --------------------------------------------------------------------------
# Policy check inputs
# --------------------------------------------------------------------------
async def quality_profile(profile_id: int) -> dict:
    r = await _client().get(f"/qualityprofile/{profile_id}")
    r.raise_for_status()
    return r.json()


async def metadata_provider_config() -> dict:
    r = await _client().get("/config/metadataprovider")
    r.raise_for_status()
    return r.json()


async def all_artists() -> list[dict]:
    r = await _client().get("/artist")
    r.raise_for_status()
    return r.json()


async def notifications() -> list[dict]:
    r = await _client().get("/notification")
    r.raise_for_status()
    return r.json()


async def ping() -> bool:
    try:
        r = await _client().get("/system/status", timeout=5.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
