"""Jellyfin, the video server: its library (movies and series with provider IDs). continuo never
carries video bytes; clients stream from Jellyfin directly (docs/design/video.md §2)."""
import httpx

from ... import config
from ...upstream import http

PAGE = 200


def _client() -> httpx.AsyncClient:
    s = config.settings
    return http.client("jellyfin", base_url=s.jellyfin_url, headers={"Authorization": api_key_auth(s.jellyfin_api_key)},
                       timeout=30.0)


def api_key_auth(key: str) -> str:
    """Jellyfin 12 takes a key only in the Authorization header (X-Emby-Token and ?api_key=
    answer 401; the query form is ?ApiKey=)."""
    return f'MediaBrowser Token="{key}"'


async def ping() -> bool:
    """True when Jellyfin answers *and* takes the API key (/System/Info/Public needs no key, so a
    wrong one would pass)."""
    try:
        r = await _client().get("/System/Info", timeout=5.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_item(item: dict) -> dict:
    ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
    return {"jellyfin_id": item["Id"], "kind": "series" if item.get("Type") == "Series" else "movie",
            "title": item.get("Name") or "", "year": item.get("ProductionYear"),
            "tmdb_id": _int(ids.get("tmdb")), "tvdb_id": _int(ids.get("tvdb")), "imdb_id": ids.get("imdb") or None}


async def library_items() -> list[dict]:
    """Every movie and series, a page at a time. A page that adds nothing not already seen ends it:
    an upstream that ignores paging (as Lidarr's /album did, continuo#84) can't loop forever."""
    out: list[dict] = []
    seen: set[str] = set()
    start = 0
    while True:
        r = await _client().get("/Items", params={
            "IncludeItemTypes": "Movie,Series", "Recursive": "true", "Fields": "ProviderIds",
            "StartIndex": start, "Limit": PAGE})
        r.raise_for_status()
        page = r.json().get("Items") or []
        new = [i for i in page if i.get("Id") and i["Id"] not in seen]
        if not new:
            break
        seen.update(i["Id"] for i in new)
        out.extend(normalize_item(i) for i in new)
        if len(page) < PAGE:
            break
        start += PAGE
    return out


# --- users and per-device tokens (docs/design/video.md §7.1) ---------------------------------

def _device_auth(device_id: str, device_name: str) -> str:
    from ... import __version__
    safe = (device_name or "device").replace('"', "'")
    return f'MediaBrowser Client="continuo", Device="{safe}", DeviceId="{device_id}", Version="{__version__}"'


async def find_user(name: str) -> dict | None:
    r = await _client().get("/Users")
    r.raise_for_status()
    return next((u for u in r.json() if (u.get("Name") or "").casefold() == name.casefold()), None)


async def create_user(name: str, password: str) -> dict:
    r = await _client().post("/Users/New", json={"Name": name, "Password": password})
    r.raise_for_status()
    return r.json()


async def authenticate(username: str, password: str, device_id: str, device_name: str) -> dict:
    """A user token for this device: {"token", "userId"}. Raises httpx.HTTPStatusError (401) on a
    wrong password."""
    r = await _client().post("/Users/AuthenticateByName", json={"Username": username, "Pw": password},
                             headers={"Authorization": _device_auth(device_id, device_name)})
    r.raise_for_status()
    body = r.json()
    return {"token": body["AccessToken"], "userId": (body.get("User") or {}).get("Id")}


async def delete_device(device_id: str) -> None:
    """Signs the device out of Jellyfin (its token stops working)."""
    r = await _client().delete("/Devices", params={"id": device_id})
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


# --- episodes (series and season pages) --------------------------------------------------------

async def episodes(series_jellyfin_id: str, user_id: str | None = None, season: int | None = None) -> list[dict]:
    """The series' episodes in Jellyfin: {season, episode, jellyfinId, title, played, positionSeconds}
    (watch state only with a user, the account's Jellyfin user)."""
    params = {"Fields": "ParentIndexNumber,IndexNumber"}
    if user_id:
        params["UserId"] = user_id
    if season is not None:
        params["Season"] = season
    r = await _client().get(f"/Shows/{series_jellyfin_id}/Episodes", params=params)
    r.raise_for_status()
    out = []
    for e in r.json().get("Items") or []:
        data = e.get("UserData") or {}
        out.append({"season": e.get("ParentIndexNumber"), "episode": e.get("IndexNumber"), "jellyfinId": e.get("Id"),
                    "title": e.get("Name"), "played": bool(data.get("Played")),
                    "positionSeconds": int((data.get("PlaybackPositionTicks") or 0) / 10_000_000)})
    return out


# --- home: continue watching, next up, recently added (per Jellyfin user) ----------------------

def summarize(item: dict) -> dict:
    """An item as the video home lists it; clients load its picture from Jellyfin
    (/Items/{jellyfinId}/Images/Primary) with their own token."""
    data = item.get("UserData") or {}
    ticks = item.get("RunTimeTicks") or 0
    kind = {"Movie": "movie", "Episode": "episode", "Series": "series"}.get(item.get("Type") or "", "other")
    ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
    return {"jellyfinId": item.get("Id"), "kind": kind, "title": item.get("Name") or "",
            "seriesTitle": item.get("SeriesName"), "seriesJellyfinId": item.get("SeriesId"),
            "season": item.get("ParentIndexNumber"), "episode": item.get("IndexNumber"),
            "year": item.get("ProductionYear"), "tmdbId": _int(ids.get("tmdb")),
            "runtimeSeconds": int(ticks / 10_000_000) if ticks else None,
            "positionSeconds": int((data.get("PlaybackPositionTicks") or 0) / 10_000_000),
            "played": bool(data.get("Played"))}


async def resume(user_id: str, limit: int = 12) -> list[dict]:
    r = await _client().get(f"/Users/{user_id}/Items/Resume", params={
        "Limit": limit, "MediaTypes": "Video", "Fields": "ProviderIds"})
    r.raise_for_status()
    return [summarize(i) for i in r.json().get("Items") or []]


async def next_up(user_id: str, limit: int = 12) -> list[dict]:
    r = await _client().get("/Shows/NextUp", params={"UserId": user_id, "Limit": limit, "Fields": "ProviderIds"})
    r.raise_for_status()
    return [summarize(i) for i in r.json().get("Items") or []]


async def latest(user_id: str, limit: int = 16) -> list[dict]:
    """Films and series, newest first. Not /Items/Latest: on Jellyfin 12 that answers nothing when
    asked for Movie,Series (it groups episodes under their series first), and only series without."""
    r = await _client().get("/Items", params={
        "userId": user_id, "Recursive": "true", "IncludeItemTypes": "Movie,Series",
        "SortBy": "DateCreated", "SortOrder": "Descending", "Limit": limit, "Fields": "ProviderIds"})
    r.raise_for_status()
    return [summarize(i) for i in r.json().get("Items") or []]
