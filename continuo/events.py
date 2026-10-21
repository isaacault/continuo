"""SSE hub (ported from lidseeker's _broadcast/sse_stream), with per-user delivery: an event
reaches its owner and admins only."""
import asyncio
import json
from dataclasses import dataclass, field


@dataclass(eq=False)
class Subscriber:
    user_id: int
    is_admin: bool
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=100))


_subscribers: set[Subscriber] = set()


def subscribe(user_id: int, is_admin: bool) -> Subscriber:
    s = Subscriber(user_id, is_admin)
    _subscribers.add(s)
    return s


def unsubscribe(s: Subscriber) -> None:
    _subscribers.discard(s)


def format_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def publish(event: str, data: dict, user_id: int | None) -> int:
    """Fire-and-forget. user_id=None means admins only (e.g. an orphaned request).
    Returns how many subscribers it reached."""
    msg = format_event(event, data)
    dead, sent = set(), 0
    for s in _subscribers:
        if not (s.is_admin or (user_id is not None and s.user_id == user_id)):
            continue
        try:
            s.queue.put_nowait(msg)
            sent += 1
        except asyncio.QueueFull:
            dead.add(s)          # a client that stopped reading; it reconnects and resyncs
    _subscribers.difference_update(dead)
    return sent


def subscriber_count() -> int:
    return len(_subscribers)
