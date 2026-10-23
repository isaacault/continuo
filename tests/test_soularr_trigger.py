"""Soularr searches Soulseek soon after a request (not at its next cycle), never mid-download."""
import asyncio

from continuo.upstream import slskd, soularr


def test_requests_together_share_one_restart_and_wait_for_slskd_to_be_idle(monkeypatch):
    restarts, busy = [], [True, True, False]

    async def has_active():
        return busy.pop(0) if busy else False

    async def restart(context):
        restarts.append(context)
        return True
    monkeypatch.setattr(soularr, "enabled", lambda: True)
    monkeypatch.setattr(soularr, "TRIGGER_DELAY_S", 0)
    monkeypatch.setattr(soularr, "BUSY_RECHECK_S", 0)
    monkeypatch.setattr(slskd, "has_active_downloads", has_active)
    monkeypatch.setattr(soularr, "try_trigger_run", restart)
    monkeypatch.setattr(soularr, "_pending", None)

    async def main():
        soularr.trigger_soon("request 1")
        soularr.trigger_soon("request 2")   # coalesced into the first
        await soularr._pending
    asyncio.run(main())
    assert restarts == ["request 1"]
    assert busy == [], "held off while slskd was downloading"


def test_gives_up_waiting_after_the_limit(monkeypatch):
    restarts, checks = [], []

    async def always_busy():
        checks.append(1)
        return True

    async def restart(context):
        restarts.append(context)
        return True
    monkeypatch.setattr(soularr, "enabled", lambda: True)
    monkeypatch.setattr(soularr, "TRIGGER_DELAY_S", 0)
    monkeypatch.setattr(soularr, "BUSY_RECHECK_S", 0)
    monkeypatch.setattr(slskd, "has_active_downloads", always_busy)
    monkeypatch.setattr(soularr, "try_trigger_run", restart)
    monkeypatch.setattr(soularr, "_pending", None)

    async def main():
        soularr.trigger_soon("request 3")
        await soularr._pending
    asyncio.run(main())
    assert len(checks) == soularr.BUSY_MAX_WAITS and restarts == ["request 3"]
