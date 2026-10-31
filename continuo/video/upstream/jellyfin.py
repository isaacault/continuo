"""Jellyfin, the video server: its library (movies and series with provider IDs). continuo never
carries video bytes; clients stream from Jellyfin directly (docs/design/video.md §2)."""
import httpx

from ... import config
from ...upstream import http

PAGE = 200


def _client() -> httpx.AsyncClient:
    s = config.settings
    return http.client("jellyfin", base_url=s.jellyfin_url, headers={"X-Emby-Token": s.jellyfin_api_key},
                       timeout=30.0)


async def ping() -> bool:
    try:
        r = await _client().get("/System/Info/Public", timeout=5.0)
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
