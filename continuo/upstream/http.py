"""Shared, connection-pooled httpx clients, one per upstream (lidseeker's shared-client pattern,
generalised). Tests swap the transport with `use_transport()` so every upstream call hits a
`httpx.MockTransport` instead of the network."""
import httpx

_clients: dict[str, httpx.AsyncClient] = {}
_transport: httpx.AsyncBaseTransport | None = None

# Bounded pools: the Jetson is small and nothing here needs more than a handful of connections.
_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5)


def client(name: str, *, base_url: str = "", headers: dict | None = None,
           timeout: float | httpx.Timeout = 30.0) -> httpx.AsyncClient:
    c = _clients.get(name)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(base_url=base_url, headers=headers or {}, timeout=timeout,
                              limits=_LIMITS, transport=_transport)
        _clients[name] = c
    return c


async def aclose() -> None:
    for c in list(_clients.values()):
        if not c.is_closed:
            await c.aclose()
    _clients.clear()


def use_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests: route every client through `transport`. Drops cached clients (without awaiting
    their close — they hold no real sockets under a mock transport)."""
    global _transport
    _transport = transport
    _clients.clear()


def reset() -> None:
    """Forget cached clients (e.g. after config.reload changed a base URL or key)."""
    _clients.clear()
