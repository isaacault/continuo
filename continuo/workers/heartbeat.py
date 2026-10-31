"""Heartbeat: push to an Uptime Kuma *push* monitor while every loop is alive, so a wedged loop
is an outage (Uptime Kuma → ntfy: the one ntfy path). Plus the nightly job: library index
refresh and policy check (Home Assistant alert only if something is wrong)."""
import logging

import httpx

from .. import config
from ..upstream import http
from . import registry

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
