"""Wikipedia's lead paragraph for an artist, found through their Wikidata item: the biography on
the catalogue artist page when Lidarr's metadata has no overview. Best-effort and keyless."""
import logging
import re
from typing import Optional
from urllib.parse import quote

import httpx

from .. import config
from . import http

log = logging.getLogger("continuo")

_QID = re.compile(r"(Q\d+)$")


def _client(name: str, base_url: str) -> httpx.AsyncClient:
    return http.client(name, base_url=base_url, timeout=15.0,
                       headers={"User-Agent": config.settings.musicbrainz_user_agent, "Accept": "application/json"})


async def summary(wikidata_url: Optional[str]) -> Optional[dict]:
    """{text, url} from English Wikipedia for a Wikidata item URL; None when there's none."""
    m = _QID.search((wikidata_url or "").rstrip("/"))
    if not m:
        return None
    qid = m.group(1)
    try:
        r = await _client("wikidata", "https://www.wikidata.org").get(f"/wiki/Special:EntityData/{qid}.json")
        r.raise_for_status()
        title = (((r.json().get("entities") or {}).get(qid) or {}).get("sitelinks") or {}).get("enwiki", {}).get("title")
        if not title:
            return None
        r = await _client("wikipedia", "https://en.wikipedia.org").get(
            f"/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}")
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        log.warning("wikipedia: summary for %s failed: %s", qid, e)
        return None
    text = (data.get("extract") or "").strip()
    if not text:
        return None
    return {"text": text, "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page")}
