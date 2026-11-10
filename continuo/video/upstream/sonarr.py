"""Sonarr, the wanted list for series (Sonarr keys on TVDB IDs; continuo gets them from TMDB).
A request monitors and searches exactly the seasons asked for (all aired seasons when none are
named; specials left out), and future episodes too, so new ones arrive as they air (the user's
choice, docs/design/video.md §12.4). It can name episodes within a season too ("picks": season 4
from episode 3 on); a pick that runs on to the season's end ("onward") takes its later episodes as
they air, by leaving the season monitored and only the episodes before unmonitored."""
import asyncio
from datetime import datetime, timezone

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


def season_flags(seasons: list[int] | None, picks: list[dict] | None) -> list[int] | None:
    """The seasons Sonarr marks monitored (which monitors episodes it adds to them later): whole ones
    and onward picks. None: every season but specials (nothing named at all)."""
    if seasons is None and not picks:
        return None
    return sorted(set(seasons or []) | {p["season"] for p in picks or [] if p.get("onward")})


def selected(episodes: list[dict], seasons: list[int] | None, picks: list[dict] | None) -> set[int]:
    """Sonarr's episode IDs a selection asks for: whole seasons (all but specials when nothing is
    named), and each pick's episodes, with the season's later ones for an onward pick."""
    whole = set(seasons or []) if (seasons is not None or picks) else None
    by_season = {p["season"]: p for p in picks or []}
    out = set()
    for e in episodes:
        n, num = e.get("seasonNumber"), e.get("episodeNumber")
        if (n in whole) if whole is not None else bool(n):
            out.add(e["id"])
        elif n in by_season:
            p = by_season[n]
            if num in p["episodes"] or (p.get("onward") and num is not None and num > max(p["episodes"])):
                out.add(e["id"])
    return out


def _aired(e: dict) -> bool:
    d = e.get("airDateUtc")
    return bool(d) and d <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def episodes(series_id: int) -> list[dict]:
    r = await _client().get("/episode", params={"seriesId": series_id})
    r.raise_for_status()
    return r.json()


async def _episodes_once_listed(series_id: int, tries: int = 30, delay: float = 2.0) -> list[dict]:
    """A series Sonarr has just added lists no episodes until its first refresh (seconds)."""
    for _ in range(tries):
        eps = await episodes(series_id)
        if eps:
            return eps
        await asyncio.sleep(delay)
    raise LookupError("Sonarr hasn't listed this series' episodes yet: ask again in a minute.")


async def _monitor(ids: set[int], monitored: bool) -> None:
    if ids:
        r = await _client().put("/episode/monitor", json={"episodeIds": sorted(ids), "monitored": monitored})
        r.raise_for_status()


async def _command(body: dict) -> None:
    r = await _client().post("/command", json=body)
    r.raise_for_status()


async def _search(series_id: int, eps: list[dict], whole: list[int], wanted: set[int]) -> None:
    """Season searches for whole seasons (season packs), an episode search for the rest wanted
    that have aired and have no file."""
    for n in whole:
        await _command({"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": n})
    rest = sorted(e["id"] for e in eps if e["id"] in wanted and e.get("seasonNumber") not in whole
                  and not e.get("hasFile") and _aired(e))
    if rest:
        await _command({"name": "EpisodeSearch", "episodeIds": rest})


async def request(tvdb_id: int, seasons: list[int] | None, future: bool = True, anime: bool = False,
                  picks: list[dict] | None = None) -> dict:
    """Adds the series (only the asked seasons and episodes monitored, searched now), or for one
    already in Sonarr monitors and searches them too. Returns Sonarr's series."""
    existing = await find(tvdb_id)
    if picks:
        return await _request_picks(tvdb_id, existing, seasons or [], picks, future, anime)
    if existing is None:
        return await _add(tvdb_id, seasons, future, anime, search=True)
    asked = seasons if seasons is not None else [x["seasonNumber"] for x in existing.get("seasons") or []
                                                 if x.get("seasonNumber", 0) > 0]
    updated = {**existing, "monitored": True,
               **({"monitorNewItems": "all"} if future else {}),
               "seasons": [{**x, "monitored": True} if x.get("seasonNumber") in asked else x
                           for x in existing.get("seasons") or []]}
    r = await _client().put(f"/series/{existing['id']}", json=updated)
    r.raise_for_status()
    for n in asked:
        c = await _client().post("/command", json={"name": "SeasonSearch", "seriesId": existing["id"], "seasonNumber": n})
        c.raise_for_status()
    return r.json()


async def _request_picks(tvdb_id: int, existing: dict | None, whole: list[int], picks: list[dict], future: bool,
                         anime: bool) -> dict:
    """request() with episodes named: added (or updated) with nothing searched, then the picked
    episodes monitored (the rest of a newly wanted season not), then searched."""
    flags = set(season_flags(whole, picks) or [])
    if existing is None:
        added = await _add(tvdb_id, sorted(flags), future, anime, search=False)
        sid, before = added["id"], {}
    else:
        sid = existing["id"]
        before = {x.get("seasonNumber"): bool(x.get("monitored")) for x in existing.get("seasons") or []}
        added = {**existing, "monitored": True, **({"monitorNewItems": "all"} if future else {}),
                 "seasons": [{**x, "monitored": True} if x.get("seasonNumber") in flags else x
                             for x in existing.get("seasons") or []]}
        r = await _client().put(f"/series/{sid}", json=added)
        r.raise_for_status()
        added = r.json()
    eps = await _episodes_once_listed(sid)
    wanted = selected(eps, whole, picks)
    pick_seasons = {p["season"] for p in picks}
    await _monitor(wanted, True)
    # A season that was already wanted keeps what it had; one newly wanted gets only the picks.
    await _monitor({e["id"] for e in eps if e.get("seasonNumber") in pick_seasons and not before.get(e.get("seasonNumber"))
                    and e["id"] not in wanted}, False)
    await _search(sid, eps, whole, wanted)
    return added


async def _add(tvdb_id: int, flags: list[int] | None, future: bool, anime: bool, search: bool) -> dict:
    s = config.settings
    lookup = await _client().get("/series/lookup", params={"term": f"tvdb:{tvdb_id}"})
    lookup.raise_for_status()
    found = next((x for x in lookup.json() if x.get("tvdbId") == tvdb_id), None)
    if found is None:
        raise LookupError("Sonarr doesn't know this series.")
    # Anime: Sonarr's anime type (absolute numbering, anime release names) in the anime folder.
    as_anime = anime and bool(s.sonarr_anime_root_folder)
    body = {**found, "seasons": wanted(found.get("seasons") or [], flags),
            "qualityProfileId": (s.sonarr_anime_quality_profile_id or s.sonarr_quality_profile_id) if as_anime
            else s.sonarr_quality_profile_id,
            "rootFolderPath": s.sonarr_anime_root_folder if as_anime else s.sonarr_root_folder,
            "seriesType": "anime" if as_anime else "standard",
            "monitored": True, "seasonFolder": True, "monitorNewItems": "all" if future else "none",
            "addOptions": {"searchForMissingEpisodes": search}}
    r = await _client().post("/series", json=body)
    r.raise_for_status()
    return r.json()


async def replace(series_id: int, seasons: list[int] | None, picks: list[dict] | None, future: bool,
                  delete_files: bool = False) -> dict:
    """Changes what a series in Sonarr is wanted for to exactly this selection: seasons and episodes
    monitored as asked and nothing else, downloads of anything else stopped, the newly wanted
    searched. delete_files: also deletes the files of episodes no longer wanted. Returns counts."""
    show = await series(series_id)
    flags = season_flags(seasons, picks)
    show = {**show, "monitored": True, "monitorNewItems": "all" if future else "none",
            "seasons": [{**x, "monitored": (x.get("seasonNumber") in flags) if flags is not None else bool(x.get("seasonNumber"))}
                        for x in show.get("seasons") or []]}
    r = await _client().put(f"/series/{series_id}", json=show)
    r.raise_for_status()
    eps = await episodes(series_id)
    want = selected(eps, seasons, picks)
    unwanted = {e["id"] for e in eps} - want
    await _monitor(want, True)
    await _monitor(unwanted, False)
    stopped = 0
    for q in await queue_for(series_id):
        ep = q.get("episodeId") or (q.get("episode") or {}).get("id")
        if ep in unwanted:
            await remove_download(q["id"])
            stopped += 1
    deleted = 0
    if delete_files:
        # A file can hold several episodes (a double episode): deleted only if none of them is wanted.
        files: dict[int, set[int]] = {}
        for e in eps:
            if e.get("episodeFileId"):
                files.setdefault(e["episodeFileId"], set()).add(e["id"])
        for file_id, in_it in files.items():
            if in_it <= unwanted:
                d = await _client().delete(f"/episodefile/{file_id}")
                d.raise_for_status()
                deleted += 1
    # Searched: what's wanted, aired and missing (whole seasons with a gap as season searches).
    whole = set(seasons or []) if (seasons is not None or picks) else {e["seasonNumber"] for e in eps if e.get("seasonNumber")}
    missing = {e["id"] for e in eps if e["id"] in want and not e.get("hasFile") and _aired(e)}
    gaps = sorted({e["seasonNumber"] for e in eps if e["id"] in missing and e.get("seasonNumber") in whole})
    await _search(series_id, eps, gaps, missing)
    return {"stoppedDownloads": stopped, "deletedFiles": deleted}


async def series(series_id: int) -> dict:
    r = await _client().get(f"/series/{series_id}")
    r.raise_for_status()
    return r.json()


async def queue_for(series_id: int) -> list[dict]:
    """The series' downloads in flight, each with its episode (season and number)."""
    r = await _client().get("/queue/details", params={"seriesId": series_id, "includeEpisode": "true"})
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, list) else body.get("records") or []


def seasons_complete(series: dict, asked: list[int] | None) -> bool:
    """Whether every asked season (all but specials when none) has a file for each of its aired
    monitored episodes, by Sonarr's statistics (its episodeCount counts only those, and any with a
    file). Not while none of them has aired: a request for episodes still to come waits for them."""
    counted = 0
    for x in series.get("seasons") or []:
        n = x.get("seasonNumber")
        if (asked is not None and n not in asked) or (asked is None and not n):
            continue
        st = x.get("statistics") or {}
        counted += st.get("episodeCount", 0)
        if st.get("episodeCount", 0) and st.get("episodeFileCount", 0) < st.get("episodeCount", 0):
            return False
    return counted > 0


async def remove_download(queue_id: int) -> None:
    r = await _client().delete(f"/queue/{queue_id}", params={"removeFromClient": "true", "blocklist": "false"})
    r.raise_for_status()


async def unmonitor_season(series_id: int, season: int) -> None:
    """Stops Sonarr wanting a season (so a stopped download isn't fetched again)."""
    show = await series(series_id)
    show["seasons"] = [{**x, "monitored": False} if x.get("seasonNumber") == season else x for x in show.get("seasons") or []]
    r = await _client().put(f"/series/{series_id}", json=show)
    r.raise_for_status()


async def delete_series(series_id: int) -> None:
    r = await _client().delete(f"/series/{series_id}", params={"deleteFiles": "true", "addImportListExclusion": "false"})
    r.raise_for_status()
