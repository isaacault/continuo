"""Sonarr, the wanted list for series (Sonarr keys on TVDB IDs; continuo gets them from TMDB).
A request monitors and searches exactly the seasons asked for (all aired seasons when none are
named; specials left out), never "future episodes too" unless asked (docs/design/video.md §12.4,
assumed)."""
import httpx

from ... import config
from ...upstream import http


def _client() -> httpx.AsyncClient:
    s = config.settings
    return http.client("sonarr", base_url=f"{s.sonarr_url}/api/v3", headers={"X-Api-Key": s.sonarr_api_key},
                       timeout=30.0)


async def ping() -> bool:
    try:
        r = await _client().get("/system/status", timeout=5.0)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def find(tvdb_id: int) -> dict | None:
    r = await _client().get("/series", params={"tvdbId": tvdb_id})
    r.raise_for_status()
    return next((s for s in r.json() if s.get("tvdbId") == tvdb_id), None)


def wanted(seasons: list[dict], asked: list[int] | None) -> list[dict]:
    """Sonarr's seasons with exactly the asked ones monitored (none asked: every season but specials)."""
    out = []
    for s in seasons:
        n = s.get("seasonNumber")
        out.append({**s, "monitored": (n in asked) if asked is not None else bool(n and n > 0)})
    return out


async def request(tvdb_id: int, seasons: list[int] | None, future: bool = False) -> dict:
    """Adds the series (only the asked seasons monitored, searched now), or for one already in
    Sonarr monitors and searches the asked seasons too. Returns Sonarr's series."""
    s = config.settings
    existing = await find(tvdb_id)
    if existing is None:
        lookup = await _client().get("/series/lookup", params={"term": f"tvdb:{tvdb_id}"})
        lookup.raise_for_status()
        found = next((x for x in lookup.json() if x.get("tvdbId") == tvdb_id), None)
        if found is None:
            raise LookupError("Sonarr doesn't know this series.")
        body = {**found, "seasons": wanted(found.get("seasons") or [], seasons),
                "qualityProfileId": s.sonarr_quality_profile_id, "rootFolderPath": s.sonarr_root_folder,
                "monitored": True, "seasonFolder": True, "monitorNewItems": "all" if future else "none",
                "addOptions": {"searchForMissingEpisodes": True}}
        r = await _client().post("/series", json=body)
        r.raise_for_status()
        return r.json()
    asked = seasons if seasons is not None else [x["seasonNumber"] for x in existing.get("seasons") or []
                                                 if x.get("seasonNumber", 0) > 0]
    updated = {**existing, "monitored": True,
               "seasons": [{**x, "monitored": True} if x.get("seasonNumber") in asked else x
                           for x in existing.get("seasons") or []]}
    r = await _client().put(f"/series/{existing['id']}", json=updated)
    r.raise_for_status()
    for n in asked:
        c = await _client().post("/command", json={"name": "SeasonSearch", "seriesId": existing["id"], "seasonNumber": n})
        c.raise_for_status()
    return r.json()


async def series(series_id: int) -> dict:
    r = await _client().get(f"/series/{series_id}")
    r.raise_for_status()
    return r.json()


async def queue_for(series_id: int) -> list[dict]:
    r = await _client().get("/queue/details", params={"seriesId": series_id})
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, list) else body.get("records") or []


def seasons_complete(series: dict, asked: list[int] | None) -> bool:
    """Whether every asked season (all but specials when none) has a file for each of its aired
    episodes, by Sonarr's statistics."""
    for x in series.get("seasons") or []:
        n = x.get("seasonNumber")
        if (asked is not None and n not in asked) or (asked is None and not n):
            continue
        st = x.get("statistics") or {}
        if st.get("episodeCount", 0) and st.get("episodeFileCount", 0) < st.get("episodeCount", 0):
            return False
    return True
