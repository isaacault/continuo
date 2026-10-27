"""LRCLIB (lrclib.net): free, open lyrics, most of them synced to the music. For songs whose files
carry none (most Soulseek downloads) and for Play now songs, which aren't in the library at all.
Only continuo calls it; the app asks continuo."""
import logging
import re
from typing import Optional

import httpx

from .. import config
from ..db import lyrics_cache
from ..requests.normalise import normalise
from . import http

log = logging.getLogger("continuo")

_URL = "https://lrclib.net/api"
_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\](.*)$")


def _client() -> httpx.AsyncClient:
    return http.client("lrclib", base_url=_URL, timeout=8.0,
                       headers={"User-Agent": config.settings.musicbrainz_user_agent, "Accept": "application/json"})


def parse_lrc(text: str) -> list[dict]:
    """[{startMs, text}] from LRC ("[01:02.34] words"), in time order; lines without a time are dropped."""
    out = []
    for raw in text.splitlines():
        m = _LRC_LINE.match(raw.strip())
        if not m:
            continue
        frac = m.group(3) or "0"
        ms = int(m.group(1)) * 60_000 + int(m.group(2)) * 1000 + int(frac.ljust(3, "0")[:3])
        out.append({"startMs": ms, "text": m.group(4).strip()})
    return sorted(out, key=lambda x: x["startMs"])


def _answer(body: dict) -> Optional[dict]:
    if body.get("instrumental"):
        return {"synced": False, "lines": [], "instrumental": True, "source": "lrclib"}
    if body.get("syncedLyrics"):
        lines = parse_lrc(body["syncedLyrics"])
        if lines:
            return {"synced": True, "lines": lines, "instrumental": False, "source": "lrclib"}
    if body.get("plainLyrics"):
        return {"synced": False, "lines": [{"startMs": None, "text": t} for t in body["plainLyrics"].splitlines()],
                "instrumental": False, "source": "lrclib"}
    return None


def cache_key(artist: str, title: str, duration_s: Optional[int]) -> str:
    return f"{normalise(artist)}|{normalise(title)}|{duration_s or ''}"


async def lyrics(artist: str, title: str, album: Optional[str] = None, duration_s: Optional[int] = None) -> Optional[dict]:
    """{synced, lines: [{startMs, text}], instrumental, source} or None when LRCLIB has none;
    cached (see db.lyrics_cache). Raises httpx errors when LRCLIB can't be asked."""
    key = cache_key(artist, title, duration_s)
    known, answer = lyrics_cache.get(key)
    if known:
        return answer
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_s:
        params["duration"] = duration_s
    r = await _client().get("/get", params=params)
    if r.status_code == 404 and album:
        # The exact album often differs (a deluxe edition, a single): try without it.
        r = await _client().get("/get", params={k: v for k, v in params.items() if k != "album_name"})
    if r.status_code == 404:
        lyrics_cache.put(key, None)
        return None
    r.raise_for_status()
    answer = _answer(r.json())
    lyrics_cache.put(key, answer)
    return answer
