"""Scrobbling Play now songs (Navidrome scrobbles library songs; it doesn't know these)."""
import dataclasses
import hashlib
import json
from urllib.parse import parse_qs

import httpx
from conftest import bearer, login, make_user

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
