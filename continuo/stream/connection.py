"""How each phone's streaming went, from the connection samples it sent (polyphony#185 phase 2,
#123), for GET /v1/admin/connection: per device, its throughput, the stalls by network kind and
at home or away, and the HLS levels it played. For looking at; nothing decides from it yet (the
stall thresholds' per-device suggestions, polyphony#14, will start from it)."""
from datetime import datetime, timezone

HLS_PROFILE = "adaptive.continuo"
RECENT = 20


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def _pct(values: list[int], q: float) -> int:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def _ms(s: dict) -> int:
    return max(0, s["ended_at_ms"] - s["started_at_ms"])


def throughput(samples: list[dict]) -> dict:
    """{n, p10, median, p90} kbit/s over the samples that measured it."""
    values = [s["throughput_kbps"] for s in samples if s["throughput_kbps"] is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "p10": _pct(values, 0.1), "median": _pct(values, 0.5),
            "p90": _pct(values, 0.9)}


def summary(samples: list[dict]) -> dict:
    """Streaming time, stalls (and per hour), and the median throughput of a group of samples."""
    ms = sum(_ms(s) for s in samples)
    stalls = sum(s["stalls"] for s in samples)
    return {
        "samples": len(samples),
        "minutes": round(ms / 60_000, 1),
        "stalls": stalls,
        "stallMs": sum(s["stall_ms"] for s in samples),
        "stallsPerHour": round(stalls / (ms / 3_600_000), 1) if ms else None,
        "medianKbps": throughput(samples).get("median"),
    }


def _place(s: dict) -> str:
    return "unknown" if s["at_home"] is None else ("home" if s["at_home"] else "away")


def _grouped(samples: list[dict], key) -> dict:
    groups: dict = {}
    for s in samples:
        groups.setdefault(key(s), []).append(s)
    return groups


def _recent(s: dict) -> dict:
    hls = s["profile_id"] == HLS_PROFILE
    return {
        "startedAt": _iso(s["started_at_ms"]), "seconds": round(_ms(s) / 1000),
        "trigger": s["trigger"], "network": s["network"], "metered": bool(s["metered"]),
        "vpn": bool(s["vpn"]), "atHome": None if s["at_home"] is None else bool(s["at_home"]),
        "responseMs": s["response_ms"], "throughputKbps": s["throughput_kbps"],
        "stalls": s["stalls"], "stallMs": s["stall_ms"],
        "profileId": s["profile_id"], "codec": s["codec"], "maxBitrateKbps": s["max_bitrate_kbps"],
        "hlsLevelKbps": s["max_bitrate_kbps"] if hls else None,
        "capped": bool(s["capped"]),
    }


def device_report(samples: list[dict]) -> dict:
    """One device's samples (oldest first, with device_name, device_platform, username)."""
    first, last = samples[0], samples[-1]
    hls = [s for s in samples if s["profile_id"] == HLS_PROFILE and s["max_bitrate_kbps"] is not None]
    levels = _grouped(hls, lambda s: s["max_bitrate_kbps"])
    return {
        "deviceId": last["device_id"],
        "name": last["device_name"],
        "platform": last["device_platform"],
        "username": last["username"],
        "signedOut": last["device_revoked_at"] is not None,
        "from": _iso(first["started_at_ms"]),
        "to": _iso(last["ended_at_ms"]),
        **summary(samples),
        "throughputKbps": throughput(samples),
        "byNetwork": {k: summary(v) for k, v in sorted(_grouped(samples, lambda s: s["network"]).items())},
        "byPlace": {k: summary(v) for k, v in sorted(_grouped(samples, _place).items())},
        "hlsLevels": [{"kbps": kbps, **summary(v)} for kbps, v in sorted(levels.items())],
        "cappedMinutes": round(sum(s["capped_ms"] for s in samples) / 60_000, 1),
        "recent": [_recent(s) for s in reversed(samples[-RECENT:])],
    }


def report(samples: list[dict]) -> list[dict]:
    """Per device, the one streamed from last first."""
    by_device = _grouped(samples, lambda s: s["device_id"])
    out = [device_report(v) for v in by_device.values()]
    return sorted(out, key=lambda d: d["to"], reverse=True)
