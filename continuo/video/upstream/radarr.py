"""Radarr, the wanted list for films (as Lidarr is for albums): continuo adds what's requested and
reads how it's going; Prowlarr and qBittorrent sit behind it."""
import httpx

from ... import config
from ...upstream import http


def _client() -> httpx.AsyncClient:
    s = config.settings
    return http.client("radarr", base_url=f"{s.radarr_url}/api/v3", headers={"X-Api-Key": s.radarr_api_key},
                       timeout=30.0)


async def ping() -> bool:
    try:
        r = await _client().get("/system/status", timeout=5.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def find(tmdb_id: int) -> dict | None:
    """The film in Radarr already, if it is."""
    r = await _client().get("/movie", params={"tmdbId": tmdb_id})
    r.raise_for_status()
    return next((m for m in r.json() if m.get("tmdbId") == tmdb_id), None)


async def add(tmdb_id: int) -> dict:
    """Adds the film, monitored, and searches for it; one already there is returned as it is."""
    existing = await find(tmdb_id)
    if existing:
        return existing
    lookup = await _client().get("/movie/lookup/tmdb", params={"tmdbId": tmdb_id})
    lookup.raise_for_status()
    movie = lookup.json()
    s = config.settings
    body = {**movie, "qualityProfileId": s.radarr_quality_profile_id, "rootFolderPath": s.radarr_root_folder,
            "monitored": True, "minimumAvailability": "released", "addOptions": {"searchForMovie": True}}
    r = await _client().post("/movie", json=body)
    r.raise_for_status()
    return r.json()


async def queue_for(movie_id: int) -> list[dict]:
    r = await _client().get("/queue/details", params={"movieId": movie_id})
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, list) else body.get("records") or []
