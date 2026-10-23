# Portions derived from lidseeker (https://github.com/IvoryCobra-VC/lidseeker), MIT License.
"""Reconcile loop (every RECONCILE_POLL_SECONDS): Lidarr's scheduled "refresh all artists" can
un-monitor a freshly added artist's album before anything grabs it. Re-assert monitoring (and
monitorNewItems=none on the artist) until the files land; a cheap no-op once stable."""
import logging

from ..db import requests as req_db
from ..upstream import lidarr

log = logging.getLogger("continuo")

_RECONCILED = ("pending", "searching", "downloading", "importing")


async def tick() -> None:
    for row in req_db.list_requests(statuses=_RECONCILED, kind="album"):
        if not (row.get("lidarr_album_id") and row.get("lidarr_artist_id")):
            continue
        try:
            before = await lidarr.get_album(row["lidarr_album_id"], fresh=True)
            artist = await lidarr.get_artist(row["lidarr_artist_id"])
            ok = await lidarr.ensure_album_monitored(row["lidarr_artist_id"],
                                                     row["lidarr_album_id"])
        except Exception:  # noqa: BLE001
            log.exception("reconcile failed for request %s", row["id"])
            continue
        drift = [what for what, bad in (
            ("album_unmonitored", not before.get("monitored")),
            ("artist_unmonitored", not artist.get("monitored")),
            ("artist_follows_new_releases", artist.get("monitorNewItems") not in (None, "none")),
        ) if bad]
        if drift and (before.get("statistics") or {}).get("trackFileCount", 0) == 0:
            req_db.add_event(row["id"], "remonitored", {"ok": ok, "drift": drift})
            log.info("request %s: Lidarr drifted (%s); re-asserted (ok=%s)",
                     row["id"], ", ".join(drift), ok)
