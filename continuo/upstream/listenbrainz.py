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
