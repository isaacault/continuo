"""Scrobbling Play now songs (Navidrome scrobbles library songs; it doesn't know these)."""
import dataclasses
import hashlib
import json
from urllib.parse import parse_qs

import httpx
from conftest import ND, bearer, login, make_user

from continuo import config
from continuo.db import streams as streams_db

LB = "https://api.listenbrainz.org/1"
FM = "https://ws.audioscrobbler.com/2.0/"


def _lastfm_key(monkeypatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, lastfm_api_key="KEY", lastfm_secret="SECRET"))


def test_listenbrainz_links_with_a_valid_token_and_never_hands_it_back(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", f"{LB}/validate-token", {"valid": True, "user_name": "isaac_lb"})
    r = client.put("/v1/scrobbling/listenbrainz", json={"token": "lb-token-123"}, headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json()["listenbrainz"] == {"available": True, "connected": True, "account": "isaac_lb"}
    assert "lb-token-123" not in r.text
    assert fake.called("GET", f"{LB}/validate-token")[0].headers["authorization"] == "Token lb-token-123"
    fake.on("GET", f"{LB}/validate-token", {"valid": False, "code": 200})
    assert client.put("/v1/scrobbling/listenbrainz", json={"token": "wrong-token"}, headers=bearer(s)).status_code == 400


def test_lastfm_needs_the_server_s_key_then_the_user_s_approval(client, fake, monkeypatch):
    make_user()
    s = login(client)
    assert client.get("/v1/scrobbling", headers=bearer(s)).json()["lastfm"]["available"] is False
    assert client.post("/v1/scrobbling/lastfm/connect", headers=bearer(s)).status_code == 409
    _lastfm_key(monkeypatch)
    calls = []

    def handler(req):
        p = dict(req.url.params) if req.method == "GET" else {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        calls.append(p)
        if p["method"] == "auth.getToken":
            return httpx.Response(200, json={"token": "tok123"})
        if len(calls) == 2:
            return httpx.Response(200, json={"error": 14, "message": "Unauthorized Token"})
        return httpx.Response(200, json={"session": {"name": "isaac_fm", "key": "SK", "subscriber": 0}})
    fake.on("GET", FM, handler=handler)
    r = client.post("/v1/scrobbling/lastfm/connect", headers=bearer(s)).json()
    assert r["token"] == "tok123" and "api_key=KEY&token=tok123" in r["url"]
    sig = calls[0]
    want = hashlib.md5(("api_keyKEY" + "methodauth.getToken" + "SECRET").encode()).hexdigest()
    assert sig["api_sig"] == want, "signed: sorted name+value pairs, then the secret"
    assert client.post("/v1/scrobbling/lastfm/finish", json={"token": "tok123"}, headers=bearer(s)).status_code == 409
    done = client.post("/v1/scrobbling/lastfm/finish", json={"token": "tok123"}, headers=bearer(s)).json()
    assert done["lastfm"] == {"available": True, "connected": True, "account": "isaac_fm"}


def test_a_play_now_song_is_scrobbled_to_what_s_linked(client, fake, monkeypatch):
    isaac = make_user()
    s = login(client)
    _lastfm_key(monkeypatch)
    from continuo.db import scrobblers
    scrobblers.put(isaac["id"], "listenbrainz", "lb-token", "isaac_lb")
    scrobblers.put(isaac["id"], "lastfm", "SK", "isaac_fm")
    row = streams_db.create("sid-1", isaac["id"], "rec-esp", "rg-sns", "Espresso", "Sabrina Carpenter", 175_000)
    fake.on("POST", f"{LB}/submit-listens", {"status": "ok"})
    fake.on("POST", FM, {"scrobbles": {}})
    r = client.post(f"/v1/play-now/{row['id']}/scrobble", json={"listenedAt": 1_793_000_000}, headers=bearer(s))
    assert r.status_code == 200 and sorted(r.json()["sent"]) == ["lastfm", "listenbrainz"]
    listen = json.loads(fake.called("POST", f"{LB}/submit-listens")[0].content)
    assert listen["listen_type"] == "single" and listen["payload"][0]["listened_at"] == 1_793_000_000
    assert listen["payload"][0]["track_metadata"]["additional_info"]["recording_mbid"] == "rec-esp"
    fm = {k: v[0] for k, v in parse_qs(fake.called("POST", FM)[0].content.decode()).items()}
    assert fm["method"] == "track.scrobble" and fm["sk"] == "SK" and fm["track"] == "Espresso" and fm["duration"] == "175"
    client.post(f"/v1/play-now/{row['id']}/scrobble", json={}, headers=bearer(s))
    assert json.loads(fake.called("POST", f"{LB}/submit-listens")[1].content)["listen_type"] == "playing_now"


def test_a_failed_scrobble_isn_t_the_app_s_problem(client, fake):
    isaac = make_user()
    s = login(client)
    from continuo.db import scrobblers
    scrobblers.put(isaac["id"], "listenbrainz", "lb-token", "x")
    row = streams_db.create("sid-2", isaac["id"], "rec", None, "T", "A", None)
    fake.on("POST", f"{LB}/submit-listens", {"code": 500}, status=500)
    r = client.post(f"/v1/play-now/{row['id']}/scrobble", json={"listenedAt": 1}, headers=bearer(s))
    assert r.status_code == 200 and r.json()["sent"] == []


# ---- where Navidrome scrobbles library songs (#122) ----

def _nd_links(fake, lb, fm, seen=None):
    def answer(service, linked):
        def handler(req):
            if seen is not None:
                seen.append((service, req.headers.get("remote-user")))
            if type(linked) is int:           # an HTTP status (a bool is an int too)
                return httpx.Response(linked, json={"error": "x"})
            return httpx.Response(200, json={"status": linked, "user": "someone"})
        return handler
    fake.on("GET", f"{ND}/api/listenbrainz/link", handler=answer("listenbrainz", lb))
    fake.on("GET", f"{ND}/api/lastfm/link", handler=answer("lastfm", fm))


def test_library_says_where_navidrome_scrobbles_asking_as_the_account(client, fake):
    make_user(navidrome="isaac_nd")
    s = login(client)
    seen = []
    _nd_links(fake, True, False, seen)
    r = client.get("/v1/scrobbling", headers=bearer(s))
    assert r.status_code == 200
    assert r.json()["library"] == {"listenbrainz": True, "lastfm": False}
    assert sorted(seen) == [("lastfm", "isaac_nd"), ("listenbrainz", "isaac_nd")]
    _nd_links(fake, False, True, seen)
    assert client.get("/v1/scrobbling", headers=bearer(s)).json()["library"] == {"listenbrainz": True, "lastfm": False}
    assert len(seen) == 2, "kept a few minutes per account"


def test_library_is_null_when_navidrome_can_t_say_and_the_endpoint_still_answers(client, fake):
    make_user()
    s = login(client)
    _nd_links(fake, 500, True)
    r = client.get("/v1/scrobbling", headers=bearer(s))
    assert r.status_code == 200 and r.json()["library"] is None
    assert r.json()["listenbrainz"]["available"] is True
    fake.on("GET", f"{ND}/api/listenbrainz/link", {"status": "yes"})   # not the shape we know
    from continuo.api import scrobbling
    scrobbling._library_failed.clear()
    assert client.get("/v1/scrobbling", headers=bearer(s)).json()["library"] is None


def test_a_service_navidrome_has_switched_off_is_not_scrobbled_there(client, fake):
    """Navidrome mounts /api/lastfm only when Last.fm is enabled server-wide: a 404 means no."""
    make_user()
    s = login(client)
    _nd_links(fake, True, 404)
    assert client.get("/v1/scrobbling", headers=bearer(s)).json()["library"] == {"listenbrainz": True, "lastfm": False}


def test_library_is_null_without_header_auth(client, fake, monkeypatch):
    make_user()
    s = login(client)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, navidrome_auth="password"))
    _nd_links(fake, True, True)
    assert client.get("/v1/scrobbling", headers=bearer(s)).json()["library"] is None
    assert not fake.called("GET", f"{ND}/api/listenbrainz/link")


def test_linking_answers_with_the_library_too(client, fake):
    make_user()
    s = login(client)
    _nd_links(fake, True, True)
    fake.on("GET", f"{LB}/validate-token", {"valid": True, "user_name": "isaac_lb"})
    r = client.put("/v1/scrobbling/listenbrainz", json={"token": "lb-token-123"}, headers=bearer(s))
    assert r.json()["library"] == {"listenbrainz": True, "lastfm": True}


def test_a_resent_listen_reaches_each_service_once(client, fake, monkeypatch):
    isaac = make_user()
    s = login(client)
    _lastfm_key(monkeypatch)
    from continuo.db import scrobblers
    scrobblers.put(isaac["id"], "listenbrainz", "lb-token", "isaac_lb")
    scrobblers.put(isaac["id"], "lastfm", "SK", "isaac_fm")
    row = streams_db.create("sid-3", isaac["id"], "rec", None, "T", "A", 200_000)
    fake.on("POST", f"{LB}/submit-listens", {"status": "ok"})
    fake.on("POST", FM, {"error": 16, "message": "The service is temporarily unavailable"})
    first = client.post(f"/v1/play-now/{row['id']}/scrobble", json={"listenedAt": 100}, headers=bearer(s)).json()
    assert first["sent"] == ["listenbrainz"]
    fake.on("POST", FM, {"scrobbles": {}})
    again = client.post(f"/v1/play-now/{row['id']}/scrobble", json={"listenedAt": 100}, headers=bearer(s)).json()
    assert sorted(again["sent"]) == ["lastfm", "listenbrainz"]
    assert len(fake.called("POST", f"{LB}/submit-listens")) == 1, "ListenBrainz had it already"
    assert len(fake.called("POST", FM)) == 2, "Last.fm failed the first time, so it's tried again"
    third = client.post(f"/v1/play-now/{row['id']}/scrobble", json={"listenedAt": 100}, headers=bearer(s)).json()
    assert sorted(third["sent"]) == ["lastfm", "listenbrainz"] and len(fake.called("POST", FM)) == 2
    client.post(f"/v1/play-now/{row['id']}/scrobble", json={"listenedAt": 400}, headers=bearer(s))
    assert len(fake.called("POST", f"{LB}/submit-listens")) == 2, "another listen of the same song is new"
