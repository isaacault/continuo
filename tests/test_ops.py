"""Health, SSE fan-out, policy check, library index, admin."""
import asyncio

from conftest import LIDARR, ND, bearer, login, make_user, run

from continuo import events
from continuo.db import users as users_db
from continuo.workers import registry


def _upstreams_ok(fake):
    fake.on("GET", f"{ND}/rest/ping", {"subsonic-response": {"status": "ok"}})
    fake.on("GET", f"{LIDARR}/system/status", {"version": "3.1.0"})


def test_health_ok_then_degraded_then_unhealthy(client, fake):
    from continuo.api import health
    _upstreams_ok(fake)
    health._upstream_cache = (0.0, {})
    registry.tick("status")
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["upstream"] == {"navidrome": "ok", "lidarr": "ok"}
    assert body["loops"]["status"].endswith("s ago")

    fake.on("GET", f"{LIDARR}/system/status", {}, status=503)
    health._upstream_cache = (0.0, {})
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "degraded"

    registry._last_tick["status"] -= 10_000          # a wedged loop
    r = client.get("/v1/health")
    assert r.status_code == 503 and r.json()["status"] == "unhealthy"


def test_sse_events_reach_owner_and_admins_only(fake):
    async def go():
        owner = events.subscribe(2, False)
        other = events.subscribe(3, False)
        admin = events.subscribe(1, True)
        sent = events.publish("request.updated", {"id": 1}, 2)
        return sent, owner.queue.qsize(), other.queue.qsize(), admin.queue.qsize()
    assert asyncio.run(go()) == (2, 1, 0, 1)


def test_policy_check_flags_upgrades_and_followers(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", f"{LIDARR}/qualityprofile/1", {"name": "FLAC", "upgradeAllowed": True})
    fake.on("GET", f"{LIDARR}/artist", [{"artistName": "Followed", "monitorNewItems": "all"},
                                       {"artistName": "Fine", "monitorNewItems": "none"}])
    fake.on("GET", f"{LIDARR}/config/metadataprovider", {"writeAudioTags": "newFiles",
                                                        "scrubAudioTags": False})
    fake.on("GET", f"{LIDARR}/notification", [{"implementation": "Webhook", "fields": [
        {"name": "url", "value": "https://music-api.ault.me/internal/webhooks/lidarr"}]}])
    fake.on("GET", f"{ND}/rest/ping", {"subsonic-response": {"status": "ok"}})
    report = client.get("/v1/admin/policy-check", headers=bearer(s)).json()
    by = {c["name"]: c for c in report["checks"]}
    assert report["ok"] is False
    assert by["quality_profile_upgrades"]["status"] == "warn"
    assert by["artists_following_new_releases"]["artists"] == ["Followed"]
    assert by["lidarr_tagging"]["status"] == "ok"
    assert by["lidarr_webhook"]["status"] == "ok"
    assert by["navidrome_auth"]["status"] == "ok"


def test_library_index_native(fake):
    from continuo.db import library as lib_db
    from continuo.workers import index
    fake.on("GET", f"{ND}/api/album", [
        {"id": "a1", "name": "Geogaddi", "albumArtist": "Boards of Canada", "maxYear": 2002,
         "songCount": 23, "mbzAlbumId": "rel-1", "mbzReleaseGroupId": "rg-1", "updatedAt": "u1"}],
        headers={"x-total-count": "1"})
    fake.on("GET", f"{ND}/api/song", [{"path": "Boards of Canada/Geogaddi (2002)/01.flac"}])
    assert run(index.refresh()) == {"mode": "native", "albums": 1, "removed": 0}
    a = lib_db.get("a1")
    assert a["folder"] == "Boards of Canada/Geogaddi (2002)"
    assert a["mbz_release_group_id"] == "rg-1"
    assert fake.called("GET", f"{ND}/api/album")[0].headers["remote-user"] == "continuo-svc"
    # Unchanged album: no second song lookup.
    run(index.refresh())
    assert len(fake.called("GET", f"{ND}/api/song")) == 1


def test_seed_admin_only_on_first_boot(fake, monkeypatch):
    from continuo import config, main
    env = dict(config.settings.__dict__, admin_user="root", admin_password="first-boot-pw")
    monkeypatch.setattr(config, "settings", config.settings.__class__(**env))
    main.seed_admin()
    assert users_db.get("root")["role"] == "admin"
    main.seed_admin()
    assert users_db.count() == 1


def test_admin_user_management(client, fake):
    make_user()
    s = login(client)
    r = client.post("/v1/admin/users", headers=bearer(s),
                    json={"username": "charlie", "password": "longenough"})
    assert r.status_code == 201
    uid = r.json()["id"]
    assert client.post("/v1/admin/users", headers=bearer(s),
                       json={"username": "charlie", "password": "longenough"}).status_code == 409
    assert client.delete("/v1/admin/users/1", headers=bearer(s)).status_code == 400
    assert client.delete(f"/v1/admin/users/{uid}", headers=bearer(s)).status_code == 200
