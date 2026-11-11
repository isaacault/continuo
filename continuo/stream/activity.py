"""What adaptive streaming has done since continuo started, for GET /v1/admin/stream.

Diagnostics only, in memory, lost on restart, and never read to decide anything: the player
adapts to the connection as it is now (polyphony#185). It answers what the phone can't show on
its own: which renditions were actually served (the player moving between levels), what was
encoded ahead and how long encodes take, and what failed.
"""
import time
from collections import Counter, deque
from datetime import datetime, timezone

RECENT = 50

_started = time.time()
_served: Counter = Counter()                    # segments served, per kbit/s
_encodes: deque = deque(maxlen=RECENT)          # finished encodes, newest first
_failed = 0
_upcoming: dict[str, dict] = {}                 # device id -> its last POST /upcoming


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reset() -> None:
    """For tests."""
    global _started, _failed
    _started = time.time()
    _served.clear()
    _encodes.clear()
    _failed = 0
    _upcoming.clear()


def segment_served(kbps: int) -> None:
    _served[kbps] += 1


def encoded(song: str, kbps: int, *, prefetch: bool, seconds: float, codec: str, segments: int) -> None:
    _encodes.appendleft({"at": _now(), "song": song, "kbps": kbps, "ahead": prefetch,
                         "seconds": round(seconds, 1), "codec": codec, "segments": segments})


def failed(song: str, kbps: int, *, prefetch: bool, error: str) -> None:
    global _failed
    _failed += 1
    _encodes.appendleft({"at": _now(), "song": song, "kbps": kbps, "ahead": prefetch,
                         "error": error[-200:]})


def upcoming(device: str, songs: int, kbps: int, lower_too: bool) -> None:
    _upcoming[device] = {"at": _now(), "songs": songs, "kbps": kbps, "lowerToo": lower_too}


def report() -> dict:
    return {
        "since": datetime.fromtimestamp(_started, timezone.utc).isoformat(timespec="seconds"),
        "segmentsServed": {str(k): n for k, n in sorted(_served.items())},
        "encodesFailed": _failed,
        "recentEncodes": list(_encodes),
        "upcoming": dict(_upcoming),
    }
