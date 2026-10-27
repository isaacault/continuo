"""Health, SSE fan-out, import from lidseeker, policy check."""
import asyncio
import sqlite3

from conftest import LIDARR, ND, bearer, login, make_user, run

from continuo import cli, events
from continuo.db import requests as req_db
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


def test_health_says_when_slskd_is_up_but_off_the_soulseek_network(client, fake, monkeypatch):
    import dataclasses

    from continuo import config
    from continuo.api import health
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, slskd_url="http://slskd.test", slskd_api_key="k"))
    _upstreams_ok(fake)
    fake.on("GET", "http://slskd.test/api/v0/application", {"version": "0.22"})
    fake.on("GET", "http://slskd.test/api/v0/server", {"state": "Connected, LoggedIn", "isLoggedIn": True})
    health._upstream_cache = (0.0, {})
    registry.tick("status")
    assert client.get("/v1/health").json()["upstream"]["soulseek"] == "ok"
    fake.on("GET", "http://slskd.test/api/v0/server", {"state": "Disconnecting", "isLoggedIn": False})
    health._upstream_cache = (0.0, {})
    body = client.get("/v1/health").json()
    assert body["upstream"]["slskd"] == "ok" and body["upstream"]["soulseek"] == "down" and body["status"] == "degraded"


def test_sse_events_reach_owner_and_admins_only(fake):
    async def go():
        owner = events.subscribe(2, False)
        other = events.subscribe(3, False)
        admin = events.subscribe(1, True)
        sent = events.publish("request.updated", {"id": 1}, 2)
        return sent, owner.queue.qsize(), other.queue.qsize(), admin.queue.qsize()
    assert asyncio.run(go()) == (2, 1, 0, 1)


def _lidseeker_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT,
                        created_at TEXT, token_version INTEGER DEFAULT 0);
    CREATE TABLE requests (id INTEGER PRIMARY KEY, type TEXT, foreign_id TEXT, title TEXT,
        artist TEXT, image_url TEXT, lidarr_artist_id INTEGER, lidarr_album_id INTEGER,
        status TEXT, error TEXT, created_at TEXT, notified INTEGER, search_attempts INTEGER,
        last_attempt_at TEXT, user_id INTEGER);
    """)
    c.execute("INSERT INTO users VALUES (1,'isaac','$2b$12$abcdefghijklmnopqrstuuMhTcRZ3xRaQdeEq9bb0o3lB0NhGKoJW','admin','2026-01-01',0)")
    rows = [
        (1, "album", "RG-AVAIL", "Owned", "A", None, 1, 11, "available", None),
        (2, "album", "rg-failed", "Gave up", "B", None, 2, 12, "failed", None),
        (3, "album", "rg-pending", "Pending", "C", None, 3, 13, "pending", None),
        (4, "album", "rg-never", "Never added", "D", None, None, None, "pending", None),
        (5, "album", "rg-error", "Errored", "E", None, None, None, "error", "boom"),
        (6, "artist", "art-1", "Whole discography", None, None, 9, None, "pending", None),
    ]
    for r in rows:
        c.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,'2026-02-01',1,0,NULL,1)", r)
    c.commit()
    c.close()


def test_import_lidseeker(fake, tmp_path):
    src = tmp_path / "lidseeker.db"
    _lidseeker_db(src)
    report = cli.import_lidseeker(str(src), match_available=False)
    assert report == {"users_added": 1, "users_skipped": 0, "requests_added": 5,
                      "requests_skipped": 0, "artist_requests_skipped": 1, "matched": 0}
    by_rg = {r["mb_release_group_id"]: r for r in req_db.list_requests()}
    assert by_rg["rg-avail"]["status"] == "available" and by_rg["rg-avail"]["notified_at"]
    assert by_rg["rg-failed"]["status"] == "searching" and by_rg["rg-failed"]["still_looking"]
    assert by_rg["rg-pending"]["status"] == "searching"
    assert by_rg["rg-never"]["status"] == "pending"            # will be re-added
    assert by_rg["rg-error"]["status"] == "error" and by_rg["rg-error"]["error"] == "boom"
    assert users_db.get("isaac")["password_hash"].startswith("$2b$12$")
    # Idempotent: a re-run at cut-over adds nothing.
    again = cli.import_lidseeker(str(src), match_available=False)
    assert again["requests_added"] == 0 and again["users_added"] == 0


def test_policy_check_flags_upgrades_and_followers(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", f"{LIDARR}/qualityprofile/1", {"name": "FLAC", "upgradeAllowed": True})
    fake.on("GET", f"{LIDARR}/artist", [{"artistName": "Followed", "monitorNewItems": "all"},
                                       {"artistName": "Fine", "monitorNewItems": "none"}])
    fake.on("GET", f"{LIDARR}/config/metadataprovider", {"writeAudioTags": "no",
                                                        "scrubAudioTags": False})
    fake.on("GET", f"{LIDARR}/notification", [{"implementation": "Webhook", "fields": [
        {"name": "url", "value": "https://music-api.ault.me/internal/webhooks/lidarr"}]}])
    fake.on("GET", f"{ND}/rest/ping", {"subsonic-response": {"status": "ok"}})
    report = client.get("/v1/admin/policy-check", headers=bearer(s)).json()
    by = {c["name"]: c for c in report["checks"]}
    assert report["ok"] is False
    assert by["quality_profile_upgrades"]["status"] == "warn"
    assert by["artists_following_new_releases"]["artists"] == ["Followed"]
    assert by["lidarr_tagging"]["status"] == "ok"            # tagging off is the safe state
    fake.on("GET", f"{LIDARR}/config/metadataprovider", {"writeAudioTags": "newFiles",
                                                        "scrubAudioTags": False})
    report = client.get("/v1/admin/policy-check", headers=bearer(s)).json()
    tagging = next(c for c in report["checks"] if c["name"] == "lidarr_tagging")
    assert tagging["status"] == "warn" and "hardlinked" in tagging["message"]
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


def test_create_user_cli(fake, monkeypatch, capsys):
    monkeypatch.setenv("CONTINUO_NEW_PASSWORD", "charlies-password")
    assert cli.main(["create-user", "charlie", "--navidrome-username", "charlie-nd"]) == 0
    u = users_db.get("charlie")
    assert u["role"] == "user" and u["navidrome_username"] == "charlie-nd"


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


def test_slow_loops_are_not_stale_between_runs(fake, monkeypatch):
    """The nightly loop wakes every 10 min; with a 5 min staleness base it must still count as
    alive 7 min after its last tick (this 503'd the first deploy)."""
    import asyncio as aio

    async def go():
        task = aio.get_running_loop().create_task(aio.sleep(3600))
        registry.register("nightly", task, lambda: 600)
        registry.register("status", aio.get_running_loop().create_task(aio.sleep(3600)),
                          lambda: 45)
        registry._last_tick["nightly"] -= 7 * 60
        registry._last_tick["status"] -= 7 * 60
        snap = registry.snapshot()
        task.cancel()
        return snap["nightly"]["stale"], snap["status"]["stale"]
    assert asyncio.run(go()) == (False, True)


def test_root_and_unknown_paths(client):
    assert client.get("/").json()["health"] == "/v1/health"
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_latency_endpoint_and_migration_backfill(client, fake):
    from continuo.db.conn import connect
    from continuo.db.migrate import available
    make_user()
    s = login(client)
    row = req_db.create(mb_release_group_id="rg-lat", title="Honeycomb", artist="Jitwam", year=2019,
                        artwork_url=None, user_id=1, created_at="2026-10-20T17:37:27+00:00")
    req_db.update(row["id"], status="available", available_at="2026-10-20T18:02:51+00:00",
                  last_source="soulseek", download_started_at=None, first_file_at=None)
    # Replay the 0002 backfill against an event log like Honeycomb's.
    with connect() as c:
        for at, to in (("2026-10-20T17:43:36+00:00", "downloading"),
                       ("2026-10-20T18:00:53+00:00", "scanning")):
            c.execute("INSERT INTO request_events (request_id, at, type, detail) VALUES (?,?,?,?)",
                      (row["id"], at, "status", '{"to":"%s"}' % to))
        sql = next(text for v, _n, text in available() if v == 2)
        import re
        code = re.sub(r"--[^\n]*", "", sql)          # the migration's comments, trailing ones too
        for stmt in code.split(";"):
            if stmt.strip().upper().startswith("UPDATE"):
                c.execute(stmt)
    got = req_db.get(row["id"])
    assert got["download_started_at"] == "2026-10-20T17:43:36+00:00"
    assert got["first_file_at"] == "2026-10-20T18:00:53+00:00"
    assert got["first_source"] == "soulseek"
    r = client.get("/v1/admin/latency?days=365", headers=bearer(s)).json()
    assert r["bySource"]["soulseek"]["toDownloadStart"]["median"] == 369.0
    assert r["recent"][0]["title"] == "Honeycomb" and r["recent"][0]["toAvailable"] == 1524.0


def test_library_index_skips_missing_songs_and_follows_a_renamed_folder(fake, tmp_path, monkeypatch):
    """Okvsho's "A Place Between Us" was renamed "A place between us" on disk; Navidrome kept the
    old songs as missing. The folder is the one on disk, and a known folder that's gone is
    looked up again."""
    import dataclasses

    from continuo import config
    from continuo.db import library as lib_db
    from continuo.workers import index
    (tmp_path / "Okvsho" / "A place between us").mkdir(parents=True)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, music_dir=str(tmp_path)))
    fake.on("GET", f"{ND}/api/album", [
        {"id": "ok", "name": "A Place Between Us", "albumArtist": "Okvsho", "songCount": 20, "updatedAt": "u1"}],
        headers={"x-total-count": "1"})
    fake.on("GET", f"{ND}/api/song", [
        {"path": "Okvsho/A Place Between Us/01 Sara Serenades the Stars (interlude).mp3", "missing": True},
        {"path": "Okvsho/A place between us/01 Sara serenades the stars (Interlude).mp3", "missing": False}])
    run(index.refresh())
    assert lib_db.get("ok")["folder"] == "Okvsho/A place between us"
    # Indexed before the fix, under the folder that's gone: looked up again though unchanged.
    lib_db.upsert_many([{**lib_db.get("ok"), "folder": "Okvsho/A Place Between Us"}])
    run(index.refresh())
    assert lib_db.get("ok")["folder"] == "Okvsho/A place between us"
