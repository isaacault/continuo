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


def tick(name: str, state: str = "ok") -> None:
    _last_tick[name] = time.monotonic()
    _state[name] = state


def register(name: str, task: asyncio.Task) -> None:
    _tasks[name] = task
    tick(name, "starting")


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
            "stale": dead or age is None or age > config.settings.loop_stale_seconds,
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
