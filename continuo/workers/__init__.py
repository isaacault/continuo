"""Background loops, started by the app lifespan."""
import asyncio

from .. import config
from . import heartbeat, reconcile, registry, status


def start() -> list[asyncio.Task]:
    # Intervals are read each iteration, so a config.reload() (tests) takes effect.
    loops = {
        "status": (lambda: config.settings.status_poll_seconds, status.tick),
        "reconcile": (lambda: config.settings.reconcile_poll_seconds, reconcile.tick),
        "heartbeat": (lambda: config.settings.heartbeat_seconds, heartbeat.beat),
        "nightly": (lambda: 600, heartbeat.nightly),
    }
    tasks = []
    for name, (interval, body) in loops.items():
        task = asyncio.get_running_loop().create_task(
            registry.run_forever(name, interval, body), name=f"continuo-{name}")
        registry.register(name, task)
        tasks.append(task)
    return tasks
