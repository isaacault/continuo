from conftest import LIDARR, ND

from continuo.workers import registry


def test_health_ok_then_degraded_then_unhealthy(client, fake):
    from continuo.api import health
    fake.on("GET", f"{ND}/rest/ping", {"subsonic-response": {"status": "ok"}})
    fake.on("GET", f"{LIDARR}/system/status", {"version": "3.1.0"})
    health._upstream_cache = (0.0, {})
    registry.tick("heartbeat")
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["upstream"] == {"navidrome": "ok", "lidarr": "ok"}

    fake.on("GET", f"{LIDARR}/system/status", {}, status=503)
    health._upstream_cache = (0.0, {})
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "degraded"

    registry._last_tick["heartbeat"] -= 10_000          # a wedged loop
    assert client.get("/v1/health").status_code == 503
