"""Film requests: added to Radarr, followed through its queue, available once Jellyfin has the film
(the video index), with an inbox notification. Series (Sonarr) come next (design §11 step 5)."""
import logging

import httpx

from .. import config
from ..notify import dispatch
from . import db, index
from .upstream import radarr, sonarr, tmdb

log = logging.getLogger("continuo")


async def start(request_id: int) -> None:
    """Hands a new request to Radarr (a film) or Sonarr (a series: the asked seasons)."""
    req = db.get(request_id)
    if not req:
        return
    if req["kind"] == "series":
        await _start_series(req)
        return
    try:
        movie = await radarr.add(req["tmdb_id"])
    except httpx.HTTPError as e:
        db.update(request_id, status="failed", error=f"Radarr: {e}")
        await _notify(db.get(request_id), "video_request_failed")
        return
    db.update(request_id, status="searching", radarr_movie_id=movie.get("id"))


async def _start_series(req: dict) -> None:
    import json
    try:
        show = await tmdb.series(req["tmdb_id"])
        tvdb = (show.get("external_ids") or {}).get("tvdb_id")
        if not tvdb:
            raise LookupError("TMDB knows no TVDB ID for this series, which Sonarr needs.")
        added = await sonarr.request(int(tvdb), json.loads(req["seasons"]) if req["seasons"] else None)
    except (httpx.HTTPError, LookupError, tmdb.Unavailable) as e:
        db.update(req["id"], status="failed", error=f"Sonarr: {e}" if isinstance(e, httpx.HTTPError) else str(e))
        await _notify(db.get(req["id"]), "video_request_failed")
        return
    db.update(req["id"], status="searching", sonarr_series_id=added.get("id"))


async def _tick_series(req: dict, refreshed: list) -> None:
    import json
    if not req["sonarr_series_id"]:
        return
    try:
        show = await sonarr.series(req["sonarr_series_id"])
        queue = await sonarr.queue_for(req["sonarr_series_id"])
    except httpx.HTTPError as e:
        log.warning("video request %s: Sonarr unreachable: %s", req["id"], e)
        return
    if sonarr.seasons_complete(show, json.loads(req["seasons"]) if req["seasons"] else None):
        if not refreshed:
            await index.refresh()
            refreshed.append(True)
        item = index.by_tmdb("series", req["tmdb_id"])
        if item is not None:
            db.update(req["id"], status="available", jellyfin_id=item["jellyfin_id"], available_at=_now())
            await _notify(db.get(req["id"]), "video_request_available")
        elif req["status"] != "importing":
            db.update(req["id"], status="importing")
    elif queue and req["status"] != "downloading":
        db.update(req["id"], status="downloading")


async def tick() -> None:
    """Moves each active request on: Radarr's or Sonarr's queue says it's downloading; once they
    have the files the index is refreshed; once Jellyfin has the title it's available."""
    refreshed = False
    series_refreshed: list = []
    for req in db.active():
        if req["kind"] == "series":
            await _tick_series(req, series_refreshed)
            continue
        item = index.by_tmdb("movie", req["tmdb_id"])
        if item is None and req["radarr_movie_id"]:
            try:
                movie = await radarr.find(req["tmdb_id"])
                queue = await radarr.queue_for(req["radarr_movie_id"])
            except httpx.HTTPError as e:
                log.warning("video request %s: Radarr unreachable: %s", req["id"], e)
                continue
            if movie and movie.get("hasFile") and not refreshed:
                await index.refresh()          # Radarr has it: see whether Jellyfin does yet
                refreshed = True
                item = index.by_tmdb("movie", req["tmdb_id"])
            elif queue and req["status"] != "downloading":
                db.update(req["id"], status="downloading")
            elif movie and movie.get("hasFile") and req["status"] != "importing":
                db.update(req["id"], status="importing")
        if item is not None:
            db.update(req["id"], status="available", jellyfin_id=item["jellyfin_id"], available_at=_now())
            await _notify(db.get(req["id"]), "video_request_available")


def _now() -> str:
    from ..db.conn import now
    return now()


_TEMPLATES = {
    "video_request_available": ("Ready to watch", "{title}"),
    "video_request_failed": ("Couldn't request it", "{title}: {error}"),
}


async def _notify(req: dict, type: str) -> None:
    title_t, body_t = _TEMPLATES[type]
    title = req.get("title") or "Film"
    if req.get("year"):
        title += f" ({req['year']})"
    action = {"type": "open_title", "kind": req["kind"], "tmdbId": req["tmdb_id"], "videoRequestId": req["id"],
              "jellyfinId": req.get("jellyfin_id"),
              "deepLink": f"{config.settings.video_deep_link_scheme}://{req['kind']}/{req['tmdb_id']}"}
    from ..db import users as users_db
    recipients = [req["user_id"]] if req.get("user_id") else sorted(users_db.admin_ids())
    for uid in recipients:
        await dispatch.to_user(uid, type=type, title=title_t, body=body_t.format(title=title, error=req.get("error") or ""),
                               action=action, tag=f"video-request-{req['id']}")
    if type == "video_request_available":
        db.update(req["id"], notified_at=_now())
