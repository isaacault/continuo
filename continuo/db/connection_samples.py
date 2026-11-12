"""connection_samples: each phone's connection while it streamed (see the migration, #123)."""
import time

from .conn import connect, now, rows

KEEP_DAYS = 28
_DAY_MS = 86_400_000

# Polyphony's ConnectionSample field -> column.
FIELDS = {
    "startedAtMs": "started_at_ms", "endedAtMs": "ended_at_ms", "trigger": "trigger",
    "network": "network", "metered": "metered", "vpn": "vpn", "atHome": "at_home",
    "responseMs": "response_ms", "networkBytes": "network_bytes", "networkMs": "network_ms",
    "throughputKbps": "throughput_kbps", "cachedBytes": "cached_bytes",
    "downloadedBytes": "downloaded_bytes", "stalls": "stalls", "stallMs": "stall_ms",
    "serverId": "server_id", "profileId": "profile_id", "codec": "codec",
    "maxBitrateKbps": "max_bitrate_kbps", "reason": "reason", "capped": "capped",
    "cappedMs": "capped_ms",
}
_COLUMNS = ", ".join(FIELDS.values())
_INSERT = (f"INSERT INTO connection_samples (device_id, received_at, {_COLUMNS}) "
           f"VALUES (?, ?, {', '.join('?' for _ in FIELDS)})")


def _cutoff_ms(now_ms: int | None = None) -> int:
    return (now_ms if now_ms is not None else int(time.time() * 1000)) - KEEP_DAYS * _DAY_MS


def add(device_id: str, samples: list[dict], now_ms: int | None = None) -> int:
    """Store [samples] (keyed like Polyphony's ConnectionSample) for the device, and drop what's
    older than KEEP_DAYS, anyone's. Returns how many were stored (older ones aren't)."""
    cutoff = _cutoff_ms(now_ms)
    keep = [s for s in samples if s["endedAtMs"] >= cutoff]
    received = now()
    with connect() as c:
        c.executemany(_INSERT, [(device_id, received, *(s[f] for f in FIELDS)) for s in keep])
        c.execute("DELETE FROM connection_samples WHERE ended_at_ms < ?", (cutoff,))
    return len(keep)


def since(since_ms: int) -> list[dict]:
    """Every sample that ended at or after [since_ms], with its device's name, platform and
    account, oldest first."""
    with connect() as c:
        return rows(c.execute(
            "SELECT s.*, d.name AS device_name, d.platform AS device_platform, "
            "d.revoked_at AS device_revoked_at, u.username "
            "FROM connection_samples s JOIN devices d ON d.id = s.device_id "
            "JOIN users u ON u.id = d.user_id "
            "WHERE s.ended_at_ms >= ? ORDER BY s.ended_at_ms, s.id", (since_ms,)))
