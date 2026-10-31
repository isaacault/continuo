"""Music's nightly work: the library index, artwork cache eviction and the policy check, once a
day at/after LIBRARY_INDEX_HOUR."""
import logging
from datetime import datetime

from .. import config
from ..upstream import homeassistant
from ..workers import index

log = logging.getLogger("continuo")


_last_nightly: str | None = None


def mark_nightly_done() -> None:
    """Startup already refreshed the index and ran the policy check; skip today's nightly run
    if the hour has already passed."""
    global _last_nightly
    if datetime.now().hour >= config.settings.library_index_hour:
        _last_nightly = datetime.now().date().isoformat()


async def nightly() -> None:
    """Runs once per local day at/after LIBRARY_INDEX_HOUR."""
    global _last_nightly
    today = datetime.now().date().isoformat()
    if _last_nightly == today or datetime.now().hour < config.settings.library_index_hour:
        return
    _last_nightly = today
    await index.refresh()
    from ..library import cache
    cache.evict()
    from .admin import policy_check
    report = await policy_check()
    problems = [c for c in report["checks"] if c["status"] == "warn"]
    if problems:
        await homeassistant.notify(
            title="continuo policy check",
            message="; ".join(c["message"] for c in problems),
            tag="continuo-policy", kind="policy")
