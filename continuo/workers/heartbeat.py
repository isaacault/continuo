"""Heartbeat: push to an Uptime Kuma *push* monitor while every loop is alive, so a wedged loop
is an outage (Uptime Kuma → ntfy: the one ntfy path). Plus the nightly job: library index
refresh and policy check (Home Assistant alert only if something is wrong)."""
import logging
from datetime import datetime

import httpx

from .. import config
from ..upstream import homeassistant, http
from . import index, registry

log = logging.getLogger("continuo")


async def beat() -> None:
    url = config.settings.uptime_kuma_push_url
    if not url or not registry.healthy():
        return
    stale = [n for n, v in registry.snapshot().items() if v["stale"]]
    try:
        await http.client("uptime-kuma", timeout=10.0).get(
            url, params={"status": "up", "msg": "OK" if not stale else "stale"})
    except httpx.HTTPError as e:
        log.warning("Uptime Kuma push failed: %s", e)


_last_nightly: str | None = None


async def nightly() -> None:
    """Runs once per local day at/after LIBRARY_INDEX_HOUR."""
    global _last_nightly
    today = datetime.now().date().isoformat()
    if _last_nightly == today or datetime.now().hour < config.settings.library_index_hour:
        return
    _last_nightly = today
    await index.refresh()
    from ..api.admin import policy_check
    report = await policy_check()
    problems = [c for c in report["checks"] if c["status"] == "warn"]
    if problems:
        await homeassistant.notify(
            title="continuo policy check",
            message="; ".join(c["message"] for c in problems),
            tag="continuo-policy", kind="policy")
