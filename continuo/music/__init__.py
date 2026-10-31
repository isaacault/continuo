"""The music part: MusicBrainz, Lidarr, Soulseek, Navidrome (index, façade, forward-auth), Play now,
scrobbling, lyrics. Its code still lives where it grew (api/, upstream/, library/, search/,
requests/, workers/); this is what the module registry mounts and starts, and nothing here is
imported unless CONTINUO_MODULES lists "music". (docs/design/video.md §11, step 1.)"""
import asyncio
from typing import Callable


def routers() -> list:
    from ..api import internal, library, playnow, requests, scrobbling, search, subsonic
    return [search.router, requests.router, internal.router, library.router, playnow.router,
            scrobbling.router, subsonic.router]


def loops() -> dict[str, tuple[Callable[[], float], Callable]]:
    from .. import config
    from ..library import completeness, links
    from ..requests import tracks
    from ..workers import heartbeat, reconcile, scan, status

    async def warm_discover() -> None:
        from ..search import discover
        await discover.warm()

    return {
        "status": (lambda: config.settings.status_poll_seconds, status.tick),
        "reconcile": (lambda: config.settings.reconcile_poll_seconds, reconcile.tick),
        "scan": (lambda: config.settings.scan_poll_seconds, scan.tick),
        "nightly": (lambda: 600, heartbeat.nightly),
        "libraries": (lambda: config.settings.library_sync_seconds, links.tick),
        "tracks": (lambda: config.settings.track_poll_seconds, tracks.tick),
        # Discovery pages built ahead (Explore, Home): checked every 20 min, rebuilt when 2.5 h old.
        "discover": (lambda: 1200, warm_discover),
        # Which library albums are complete (GET /v1/library/partial): a few counted every 5 min.
        "completeness": (lambda: 300, completeness.tick),
    }


def on_start() -> list[asyncio.Task]:
    """Requests and Play now streams a restart left mid-way, and the first library index."""
    from ..requests import orchestrate, playnow
    orchestrate.requeue_stranded()
    playnow.requeue_interrupted()
    return [asyncio.get_running_loop().create_task(_startup_checks())]


async def _startup_checks() -> None:
    import logging

    from ..api import admin
    from ..workers import heartbeat, index
    log = logging.getLogger("continuo")
    try:
        await index.refresh()
    except Exception as e:  # noqa: BLE001 — Navidrome may still be starting
        log.warning("initial library index failed: %s", e)
    try:
        report = await admin.policy_check()
        for c in report["checks"]:
            if c["status"] != "ok":
                log.warning("policy check %s: %s — %s", c["name"], c["status"], c["message"])
    except Exception:  # noqa: BLE001
        log.exception("startup policy check failed")
    heartbeat.mark_nightly_done()   # this was today's index + policy check; don't repeat it


def sign_in(user: dict, device: dict) -> dict:
    """What the sign-in answer carries for music: the Subsonic credential."""
    from ..api.auth import library_credential
    return {"library": library_credential(user, device)}
