"""Loop liveness. Each background loop calls `tick(name)` once per iteration; health and the
heartbeat report a loop as stale when it hasn't ticked within LOOP_STALE_SECONDS. (Lidseeker's
health only checked the DB, so a dead loop went unnoticed.)"""
import asyncio
import logging
import time

from .. import config

log = logging.getLogger("continuo")

_last_tick: dict[str, float] = {}
_state: dict[str, str] = {}
_tasks: dict[str, asyncio.Task] = {}
_intervals: dict[str, object] = {}


def tick(name: str, state: str = "ok") -> None:
    _last_tick[name] = time.monotonic()
    _state[name] = state


def register(name: str, task: asyncio.Task, interval=None) -> None:
    _tasks[name] = task
    if interval is not None:
        _intervals[name] = interval
    tick(name, "starting")


def stale_after(name: str) -> float:
    """A loop is stale once it misses LOOP_STALE_SECONDS or two of its own intervals, whichever
    is longer — a loop that sleeps 10 min between runs isn't dead after 5."""
    base = config.settings.loop_stale_seconds
    interval = _intervals.get(name)
    return max(base, 2 * interval() + 60) if interval else base


def snapshot() -> dict[str, dict]:
    now = time.monotonic()
    out = {}
    for name in sorted(set(_last_tick) | set(_tasks)):
        age = now - _last_tick[name] if name in _last_tick else None
        task = _tasks.get(name)
        dead = task is not None and task.done()
        out[name] = {
            "ageSeconds": None if age is None else round(age, 1),
            "state": "dead" if dead else _state.get(name, "unknown"),
            "stale": dead or age is None or age > stale_after(name),
        }
    return out


def healthy() -> bool:
    return not any(v["stale"] for v in snapshot().values())


def human(age: float | None) -> str:
    if age is None:
        return "never"
    if age < 90:
        return f"{int(age)}s ago"
    return f"{int(age // 60)}m ago"


async def run_forever(name: str, interval, body) -> None:
    """Run `body()` every `interval()` seconds forever; never let an exception kill the loop."""
    while True:
        try:
            await body()
            tick(name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a loop must survive anything
            log.exception("%s loop error", name)
            tick(name, "error")
        await asyncio.sleep(interval())


def reset() -> None:
    """Tests only."""
    _last_tick.clear()
    _state.clear()
    _tasks.clear()
    _intervals.clear()
