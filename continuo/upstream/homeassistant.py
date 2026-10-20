"""Home Assistant push: POST to a webhook-triggered automation (deploy/homeassistant/
music-requests.yaml), which calls notify.mobile_app_<phone> with the deep link as clickAction.
Same pattern as torrent-cleanup's alerts, separate webhook so it gets its own channel."""
import logging

import httpx

from .. import config
from . import http

log = logging.getLogger("continuo")


def enabled() -> bool:
    return bool(config.settings.ha_webhook_url)


async def notify(*, title: str, message: str, tag: str, click_action: str | None = None,
                 kind: str = "request", username: str | None = None) -> bool:
    """True when Home Assistant accepted the webhook. Never raises."""
    if not enabled():
        return False
    payload = {"title": title, "message": message, "tag": tag, "kind": kind,
               "clickAction": click_action, "username": username}
    try:
        r = await http.client("homeassistant", timeout=15.0).post(
            config.settings.ha_webhook_url, json=payload)
        if r.status_code >= 300:
            log.warning("Home Assistant webhook returned %s", r.status_code)
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("Home Assistant webhook failed: %s", e)
        return False
