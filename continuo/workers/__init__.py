"""Background loops, started by the app lifespan."""
import asyncio

from .. import config
from . import heartbeat, registry


def loop_specs(parts: list) -> dict:
    """core's loops, then each enabled part's. Intervals are read each iteration, so a
    config.reload() (tests) takes effect."""
    loops = {"heartbeat": (lambda: config.settings.heartbeat_seconds, heartbeat.beat)}
    for part in parts:
        loops.update(part.loops())
    return loops


def start(parts: list) -> list[asyncio.Task]:
    loops = loop_specs(parts)
    tasks = []
    for name, (interval, body) in loops.items():
        task = asyncio.get_running_loop().create_task(
            registry.run_forever(name, interval, body), name=f"continuo-{name}")
        registry.register(name, task, interval)
        tasks.append(task)
    return tasks
