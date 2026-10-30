"""Background loops, started by the app lifespan."""
import asyncio

from .. import config
from ..library import completeness, links
from ..requests import tracks
from . import heartbeat, reconcile, registry, scan, status


async def _warm_discover() -> None:
    from ..search import discover
    await discover.warm()


def start() -> list[asyncio.Task]:
    # Intervals are read each iteration, so a config.reload() (tests) takes effect.
    loops = {
        "status": (lambda: config.settings.status_poll_seconds, status.tick),
        "reconcile": (lambda: config.settings.reconcile_poll_seconds, reconcile.tick),
        "scan": (lambda: config.settings.scan_poll_seconds, scan.tick),
        "heartbeat": (lambda: config.settings.heartbeat_seconds, heartbeat.beat),
        "nightly": (lambda: 600, heartbeat.nightly),
        "libraries": (lambda: config.settings.library_sync_seconds, links.tick),
        "tracks": (lambda: config.settings.track_poll_seconds, tracks.tick),
        # Discovery pages built ahead (Explore, Home): checked every 20 min, rebuilt when 2.5 h old.
        "discover": (lambda: 1200, _warm_discover),
        # Which library albums are complete (GET /v1/library/partial): a few counted every 5 min.
        "completeness": (lambda: 300, completeness.tick),
    }
    tasks = []
    for name, (interval, body) in loops.items():
        task = asyncio.get_running_loop().create_task(
            registry.run_forever(name, interval, body), name=f"continuo-{name}")
        registry.register(name, task, interval)
        tasks.append(task)
    return tasks
