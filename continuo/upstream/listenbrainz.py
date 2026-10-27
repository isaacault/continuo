"""ListenBrainz: an artist's similar artists and top songs, for the catalogue artist page.

Similar artists come from the labs API (open). Top songs come from the popularity API, which since
2025 needs a user token (scrapers); without LISTENBRAINZ_TOKEN that part is skipped. Both are
best-effort: an error means an empty section, never a failed page."""
import logging
from typing import Optional

import httpx

from .. import config
from . import http

log = logging.getLogger("continuo")

_API_URL = "https://api.listenbrainz.org/1"
_LABS_URL = "https://labs.api.listenbrainz.org"
# The algorithm ListenBrainz's own artist pages use for "similar artists".
_SIMILAR_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"


def _client(name: str, base_url: str, token: str = "") -> httpx.AsyncClient:
    headers = {"User-Agent": config.settings.musicbrainz_user_agent, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"
    return http.client(name, base_url=base_url, timeout=15.0, headers=headers)


def has_token() -> bool:
    return bool(config.settings.listenbrainz_token)


async def similar_artists(artist_mbid: str, limit: int = 12) -> Optional[list[dict]]:
    """[{musicBrainzArtistId, name, comment}] most similar first; None when ListenBrainz failed."""
    try:
        r = await _client("listenbrainz-labs", _LABS_URL).get(
            "/similar-artists/json", params={"artist_mbids": artist_mbid, "algorithm": _SIMILAR_ALGORITHM})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: similar artists for %s failed: %s", artist_mbid, e)
        return None
    out = []
    for a in data if isinstance(data, list) else []:
        mbid, name = a.get("artist_mbid"), a.get("name")
        if mbid and name and mbid != artist_mbid:
            out.append({"musicBrainzArtistId": mbid, "name": name, "comment": a.get("comment") or None})
    return out[:limit]


async def artist_metadata(artist_mbid: str) -> Optional[dict]:
    """ListenBrainz's artist metadata: name, and its links (rels) such as wikidata."""
    try:
        r = await _client("listenbrainz", _API_URL).get("/metadata/artist/", params={"artist_mbids": artist_mbid})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: metadata for %s failed: %s", artist_mbid, e)
        return None
    return data[0] if isinstance(data, list) and data else None


async def top_recordings(artist_mbid: str, limit: int = 10) -> Optional[list[dict]]:
    """[{title, artist, durationMs, recordingMbid, releaseMbid, releaseName}] by listen count;
    None without a token or when ListenBrainz failed."""
    token = config.settings.listenbrainz_token
    if not token:
        return None
    try:
        r = await _client("listenbrainz-auth", _API_URL, token).get(
            f"/popularity/top-recordings-for-artist/{artist_mbid}")
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: top songs for %s failed: %s", artist_mbid, e)
        return None
    out = []
    for t in data if isinstance(data, list) else []:
        title = t.get("recording_name")
        if not title:
            continue
        out.append({
            "title": title,
            "artist": t.get("artist_name"),
            "durationMs": t.get("length") or None,
            "recordingMbid": t.get("recording_mbid"),
            "releaseMbid": t.get("release_mbid"),
            "releaseName": t.get("release_name"),
        })
    return out[:limit]


async def _popularity(kind: str, key: str, mbids: list[str]) -> dict[str, int]:
    """Total listens per MBID ({} when ListenBrainz failed): [kind] is artist, release-group or recording."""
    mbids = [m for m in dict.fromkeys(mbids) if m]
    if not mbids:
        return {}
    token = config.settings.listenbrainz_token
    try:
        r = await _client("listenbrainz-auth" if token else "listenbrainz", _API_URL, token).post(
            f"/popularity/{kind}", json={f"{key}_mbids": mbids})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: %s popularity failed: %s", kind, e)
        return {}
    return {x.get(f"{key}_mbid"): int(x.get("total_listen_count") or 0) for x in data if isinstance(x, dict)}


async def artist_popularity(mbids: list[str]) -> dict[str, int]:
    return await _popularity("artist", "artist", mbids)


async def release_group_popularity(mbids: list[str]) -> dict[str, int]:
    return await _popularity("release-group", "release_group", mbids)


async def recording_popularity(mbids: list[str]) -> dict[str, int]:
    return await _popularity("recording", "recording", mbids)


async def top_release_groups(artist_mbid: str, limit: int = 5) -> Optional[list[dict]]:
    """An artist's most listened albums: [{musicBrainzReleaseGroupId, title, year, albumType,
    caaReleaseMbid, listens}]; None without a token or when ListenBrainz failed."""
    token = config.settings.listenbrainz_token
    if not token:
        return None
    try:
        r = await _client("listenbrainz-auth", _API_URL, token).get(
            f"/popularity/top-release-groups-for-artist/{artist_mbid}")
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: top albums for %s failed: %s", artist_mbid, e)
        return None
    out = []
    for x in data if isinstance(data, list) else []:
        rg = x.get("release_group") or {}
        if not x.get("release_group_mbid") or not rg.get("name"):
            continue
        date = rg.get("date") or ""
        out.append({"musicBrainzReleaseGroupId": x["release_group_mbid"], "title": rg["name"],
                    "year": int(date[:4]) if date[:4].isdigit() else None, "albumType": rg.get("type"),
                    "caaReleaseMbid": rg.get("caa_release_mbid"), "listens": int(x.get("total_listen_count") or 0)})
    return out[:limit]


async def lookup_recordings(pairs: list[tuple[str, str]]) -> list[Optional[dict]]:
    """MusicBrainz IDs for (artist, title) pairs, one call: [{recordingMbid, recordingName,
    artistCreditName, artistMbids, releaseMbid, releaseName} or None], in order. ListenBrainz's
    lookup matches loosely; the caller checks the names. All None without a token or on failure."""
    token = config.settings.listenbrainz_token
    if not token or not pairs:
        return [None] * len(pairs)
    try:
        r = await _client("listenbrainz-auth", _API_URL, token).post(
            "/metadata/lookup/", json={"recordings": [{"artist_name": a, "recording_name": t} for a, t in pairs]})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: lookup failed: %s", e)
        return [None] * len(pairs)
    out: list[Optional[dict]] = [None] * len(pairs)
    for x in data if isinstance(data, list) else []:
        i = x.get("index")
        if isinstance(i, int) and 0 <= i < len(pairs) and x.get("recording_mbid"):
            out[i] = {"recordingMbid": x["recording_mbid"], "recordingName": x.get("recording_name"),
                      "artistCreditName": x.get("artist_credit_name"), "artistMbids": x.get("artist_mbids") or [],
                      "releaseMbid": x.get("release_mbid"), "releaseName": x.get("release_name")}
    return out


async def recording_releases(recording_mbids: list[str]) -> dict[str, dict]:
    """Each recording's release, one call: {mbid: {releaseGroupMbid, name, year, caaReleaseMbid}};
    {} without a token or on failure."""
    token = config.settings.listenbrainz_token
    ids = [m for m in dict.fromkeys(recording_mbids) if m]
    if not token or not ids:
        return {}
    try:
        r = await _client("listenbrainz-auth", _API_URL, token).get(
            "/metadata/recording/", params={"recording_mbids": ",".join(ids), "inc": "release"})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("listenbrainz: recording metadata failed: %s", e)
        return {}
    out = {}
    for mbid, v in (data.items() if isinstance(data, dict) else []):
        rel = (v or {}).get("release") or {}
        if rel.get("release_group_mbid"):
            out[mbid] = {"releaseGroupMbid": rel["release_group_mbid"], "name": rel.get("name"),
                         "year": rel.get("year"), "caaReleaseMbid": rel.get("caa_release_mbid")}
    return out

