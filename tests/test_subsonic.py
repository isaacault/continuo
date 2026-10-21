"""The façade's getOpenSubsonicExtensions (issue #4): Navidrome's list minus what forward_auth
can't honour, unauthenticated, in the format the client asked for."""
import json
import pathlib
import re
import xml.etree.ElementTree as ET

import httpx
from conftest import ND

EXT_URL = f"{ND}/rest/getOpenSubsonicExtensions"
NS = "{http://subsonic.org/restapi}"

# Navidrome v0.64.2's answer (server/subsonic/opensubsonic.go), plus apiKeyAuthentication,
# which Navidrome's master branch already advertises.
NAVIDROME_064 = {"subsonic-response": {
    "status": "ok", "version": "1.16.1", "type": "navidrome", "serverVersion": "0.64.2 (abc1234)",
    "openSubsonic": True, "openSubsonicExtensions": [
        {"name": "transcodeOffset", "versions": [1]},
        {"name": "formPost", "versions": [1]},
        {"name": "songLyrics", "versions": [1, 2]},
        {"name": "indexBasedQueue", "versions": [1]},
        {"name": "transcoding", "versions": [1]},
        {"name": "playbackReport", "versions": [1]},
        {"name": "topSongsByArtistId", "versions": [1]},
        {"name": "apiKeyAuthentication", "versions": [1]},
    ]}}
KEPT = ["transcodeOffset", "songLyrics", "indexBasedQueue", "transcoding", "playbackReport",
        "topSongsByArtistId"]


def test_withholds_formpost_and_keeps_the_rest(client, fake):
    fake.on("GET", EXT_URL, NAVIDROME_064)
    # No u at all: the endpoint is public by spec. Credentials, if a client sends them anyway,
    # are not passed on to Navidrome.
    r = client.get("/rest/getOpenSubsonicExtensions.view?f=json&v=1.13.0&c=Feishin"
                   "&u=isaac&t=deadbeef&s=salt", headers={"Remote-User": "admin"})
    assert r.status_code == 200
    body = r.json()["subsonic-response"]
    names = [e["name"] for e in body["openSubsonicExtensions"]]
    assert names == KEPT
    assert body["openSubsonicExtensions"][1] == {"name": "songLyrics", "versions": [1, 2]}
    assert (body["status"], body["type"], body["serverVersion"], body["openSubsonic"]) == \
        ("ok", "navidrome", "0.64.2 (abc1234)", True)
    (call,) = fake.called("GET", EXT_URL)
    assert dict(call.url.params) == {"v": "1.16.1", "c": "continuo", "f": "json"}
    assert "remote-user" not in call.headers


def test_plain_path_and_form_post(client, fake):
    fake.on("GET", EXT_URL, NAVIDROME_064)
    r = client.post("/rest/getOpenSubsonicExtensions", content="f=json&v=1.16.1&c=x",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    assert "formPost" not in json.dumps(r.json())


def test_xml_is_the_default(client, fake):
    fake.on("GET", EXT_URL, NAVIDROME_064)
    r = client.get("/rest/getOpenSubsonicExtensions?v=1.16.1&c=x")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(r.content)
    assert root.tag == f"{NS}subsonic-response"
    assert root.get("status") == "ok" and root.get("openSubsonic") == "true"
    assert root.get("type") == "navidrome"
    exts = root.findall(f"{NS}openSubsonicExtensions")
    assert [e.get("name") for e in exts] == KEPT
    assert [v.text for v in exts[1].findall(f"{NS}versions")] == ["1", "2"]


def test_jsonp(client, fake):
    fake.on("GET", EXT_URL, NAVIDROME_064)
    r = client.get("/rest/getOpenSubsonicExtensions?f=jsonp&callback=cb_1")
    assert r.text.startswith("cb_1({") and "formPost" not in r.text
    # Not a valid JS identifier → plain JSON rather than echoing it into a script.
    r = client.get("/rest/getOpenSubsonicExtensions?f=jsonp&callback=alert(1)//")
    assert r.json()["subsonic-response"]["status"] == "ok"


def test_navidrome_down_is_a_subsonic_error(client, fake):
    fake.on("GET", EXT_URL, handler=lambda _r: httpx.Response(503, text="starting"))
    r = client.get("/rest/getOpenSubsonicExtensions?f=json")
    assert r.status_code == 502
    assert r.json()["subsonic-response"]["status"] == "failed"
    r = client.get("/rest/getOpenSubsonicExtensions")
    assert r.status_code == 502 and b'status="failed"' in r.content


def test_forward_auth_still_refuses_body_credentials(client):
    # What the Caddy change avoids: forward_auth sees no body, so a formPost request has no u.
    r = client.get("/internal/auth/subsonic",
                   headers={"X-Forwarded-Uri": "/rest/ping.view", "X-Forwarded-Method": "POST"})
    assert r.status_code == 401 and b'code="10"' in r.content


def test_caddy_snippet_routes_extensions_to_continuo_without_auth():
    """The snippet must send getOpenSubsonicExtensions to continuo (or clients see Navidrome's
    formPost), without forward_auth (the spec makes it public), ahead of the /rest/* façade."""
    snippet = (pathlib.Path(__file__).parent.parent / "deploy/caddy/Caddyfile.snippet").read_text()
    live = "\n".join(line for line in snippet.splitlines() if not line.lstrip().startswith("#"))
    m = re.search(r"handle /rest/getOpenSubsonicExtensions\* \{(.*?)\n\t\}", live, re.S)
    assert m, "no handle /rest/getOpenSubsonicExtensions* block"
    block = m.group(1)
    assert "reverse_proxy continuo:8080" in block
    assert "forward_auth" not in block and "navidrome" not in block
    assert live.index("handle /rest/getOpenSubsonicExtensions*") < live.index("handle /rest/* {")
