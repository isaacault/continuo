"""/v1/artwork (issue #13): catalogue art served by continuo, never an open proxy."""
import dataclasses
import os
from urllib.parse import quote

import httpx
import pytest
from conftest import LIDARR, bearer, login, make_user

from continuo import artwork, config
from continuo.db import requests as req_db

RG = "4a1b2c3d-0000-4000-8000-000000000001"
REL = "31765b9f-e969-4257-855f-c7ea1f657b2a"
CAA = f"https://coverartarchive.org/release-group/{RG}/front-500"
ARCHIVE = f"https://archive.org/download/mbid-{REL}/mbid-{REL}-1_thumb500.jpg"
IA = f"https://ia800505.us.archive.org/1/items/mbid-{REL}/mbid-{REL}-1_thumb500.jpg"
MIRROR = f"https://images.lidarr.audio/cache/{CAA}"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _image(body=JPEG, ctype="image/jpeg", **headers):
    return lambda _r: httpx.Response(200, content=body,
                                     headers={"content-type": ctype, **headers})


def _redirect(to, status=307):
    return lambda _r: httpx.Response(status, headers={"location": to})


def _caa_chain(fake):
    """What Cover Art Archive really does: 307 to archive.org, which 302s to a storage node."""
    fake.on("GET", CAA, handler=_redirect(ARCHIVE))
    fake.on("GET", ARCHIVE, handler=_redirect(IA, 302))
    fake.on("GET", IA, handler=_image())


def _settings(monkeypatch, **kw):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, **kw))


# --------------------------------------------------------------------------
# The allow-list
# --------------------------------------------------------------------------
@pytest.mark.parametrize("src", [
    CAA,
    f"https://coverartarchive.org/release/{REL}/123-250.jpg",
    "https://images.lidarr.audio/cache/abc.jpg",
    "https://imagecache.lidarr.audio/v1/caa/abc/def-500.jpg",
])
def test_allowed_sources(src):
    assert artwork.check_source(src)
    assert artwork.public_url(src) == f"https://music-api.test/v1/artwork?src={quote(src, safe='')}"


@pytest.mark.parametrize("src", [
    f"http://coverartarchive.org/release-group/{RG}/front-500",        # not https
    "https://evil.example/cover.jpg",                                    # not allow-listed
    "https://coverartarchive.org.evil.example/cover.jpg",                # suffix trick
    "https://evilcoverartarchive.org/cover.jpg",
    ARCHIVE,                                                             # redirect-only host
    IA,
    "https://user:pw@coverartarchive.org/x",                             # credentials
    "https://coverartarchive.org:8443/x",                                # other port
    "file:///etc/passwd",
    "https://169.254.169.254/latest/meta-data",
    "not a url",
])
def test_refused_sources(src):
    with pytest.raises(artwork.Refused):
        artwork.check_source(src)
    assert artwork.public_url(src) is None


def test_open_proxy_refused_without_any_upstream_call(client, fake):
    for src in ("https://evil.example/cover.jpg", "http://coverartarchive.org/x",
                "http://lidarr.test/api/v1/system/status", ARCHIVE):
        r = client.get("/v1/artwork", params={"src": src})
        assert r.status_code == 400, src
        assert r.json()["error"]["code"] == "artwork_not_allowed"
    assert fake.calls == []


def test_redirect_off_the_allow_list_is_not_followed(client, fake):
    fake.on("GET", CAA, handler=_redirect("https://evil.example/cover.jpg"))
    r = client.get("/v1/artwork", params={"src": CAA})
    assert r.status_code == 502
    assert fake.called("GET", "https://evil.example/cover.jpg") == []


def test_http_redirect_is_upgraded_to_https(client, fake):
    fake.on("GET", CAA, handler=_redirect(ARCHIVE.replace("https://", "http://")))
    fake.on("GET", ARCHIVE, handler=_image())
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 200
    assert all(c.url.scheme == "https" for c in fake.calls)


def test_redirect_loop_is_bounded(client, fake):
    fake.on("GET", CAA, handler=_redirect(CAA))
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 502
    assert len(fake.called("GET", CAA)) == artwork.MAX_REDIRECTS + 1


# --------------------------------------------------------------------------
# Serving and caching
# --------------------------------------------------------------------------
def test_follows_caa_redirects_serves_and_caches(client, fake):
    _caa_chain(fake)
    r = client.get("/v1/artwork", params={"src": CAA})     # no Authorization: Coil sends none
    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "public, max-age=604800"
    assert [str(c.url) for c in fake.calls] == [CAA, ARCHIVE, IA]
    assert os.listdir(artwork.cache_dir()) == [artwork.cache_key(httpx.URL(CAA))]

    fake.calls.clear()
    again = client.get("/v1/artwork", params={"src": CAA})
    assert again.status_code == 200 and again.content == JPEG
    assert fake.calls == []                                 # served from disk
    etag = again.headers["etag"]
    assert client.get("/v1/artwork", params={"src": CAA},
                      headers={"If-None-Match": etag}).status_code == 304


def test_cache_survives_a_restart(client, fake):
    _caa_chain(fake)
    client.get("/v1/artwork", params={"src": CAA})
    artwork.reset()
    fake.calls.clear()
    assert client.get("/v1/artwork", params={"src": CAA}).content == JPEG
    assert fake.calls == []


def test_size_picks_a_caa_thumbnail(client, fake):
    big = CAA.replace("front-500", "front-1200")
    fake.on("GET", big, handler=_image(PNG, "image/png"))
    r = client.get("/v1/artwork", params={"src": CAA, "size": 900})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert [str(c.url) for c in fake.calls] == [big]
    assert str(artwork.sized(httpx.URL(CAA), 100)).endswith("/front-250")
    assert str(artwork.sized(httpx.URL(CAA), 5000)).endswith("/front-1200")
    lidarr_img = httpx.URL("https://images.lidarr.audio/cache/abc.jpg")
    assert artwork.sized(lidarr_img, 250) == lidarr_img


def test_missing_art_is_remembered(client, fake):
    fake.on("GET", CAA, status=404)
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 404
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 404
    assert len(fake.calls) == 1


@pytest.mark.parametrize("ctype,body", [
    ("text/html", b"<html>hi</html>"),
    ("image/svg+xml", b"<svg onload='alert(1)'/>"),
    ("application/octet-stream", JPEG),
    ("image/jpeg", b"<html>not really a jpeg</html>"),
])
def test_only_images_are_served(client, fake, ctype, body):
    fake.on("GET", CAA, handler=_image(body, ctype))
    r = client.get("/v1/artwork", params={"src": CAA})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "artwork_unusable"
    assert not os.path.isdir(artwork.cache_dir()) or os.listdir(artwork.cache_dir()) == []


def test_response_size_is_capped(client, fake, monkeypatch):
    _settings(monkeypatch, artwork_max_bytes=100)
    fake.on("GET", CAA, handler=_image())                   # 204 bytes > 100
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 502
    # Also when the upstream lies about (or omits) its length: the streamed body is counted.
    chunked = httpx.Response(200, headers={"content-type": "image/jpeg"},
                             stream=httpx.ByteStream(JPEG))
    fake.on("GET", CAA, handler=lambda _r: chunked)
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 502
    assert not os.path.isdir(artwork.cache_dir()) or os.listdir(artwork.cache_dir()) == []


def test_upstream_down_is_a_retryable_502(client, fake):
    def boom(req):
        raise httpx.ConnectError("unreachable", request=req)
    fake.on("GET", CAA, handler=boom)
    r = client.get("/v1/artwork", params={"src": CAA})
    assert r.status_code == 502
    assert r.json()["error"] == {"code": "artwork_unavailable", "retryable": True,
                                 "message": "The artwork source is unreachable."}


# --------------------------------------------------------------------------
# archive.org unreachable (issue #15): Lidarr's mirror of Cover Art Archive
# --------------------------------------------------------------------------
def _unreachable(req):
    raise httpx.ConnectTimeout("archive.org timed out", request=req)


def test_mirror_url_only_for_caa_images():
    assert str(artwork.mirror(httpx.URL(CAA))) == MIRROR
    rel = f"https://coverartarchive.org/release/{REL}/123-250.jpg"
    assert str(artwork.mirror(httpx.URL(rel))) == f"https://images.lidarr.audio/cache/{rel}"
    for other in ("https://images.lidarr.audio/cache/abc.jpg",
                  f"https://coverartarchive.org/release-group/{RG}",           # JSON index
                  f"{CAA}?x=https://evil.example/", f"{CAA}#frag",
                  "https://coverartarchive.org/../../etc/passwd"):
        assert artwork.mirror(httpx.URL(other)) is None, other


def test_archive_org_unreachable_falls_back_to_lidarr_mirror(client, fake, monkeypatch):
    fake.on("GET", CAA, handler=_redirect(ARCHIVE))
    fake.on("GET", ARCHIVE, handler=_unreachable)
    fake.on("GET", MIRROR, handler=_image())
    r = client.get("/v1/artwork", params={"src": CAA})
    assert r.status_code == 200 and r.content == JPEG
    assert [str(c.url) for c in fake.calls] == [CAA, ARCHIVE, MIRROR]
    # Cached under the source the client named, so it's served from disk once archive.org is back.
    assert os.listdir(artwork.cache_dir()) == [artwork.cache_key(httpx.URL(CAA))]

    # While archive.org is down, the next cover goes to the mirror first: no connect timeout.
    other = CAA.replace("front-500", "front-250")
    fake.on("GET", f"https://images.lidarr.audio/cache/{other}", handler=_image(PNG, "image/png"))
    fake.calls.clear()
    assert client.get("/v1/artwork", params={"src": other}).status_code == 200
    assert [str(c.url) for c in fake.calls] == [f"https://images.lidarr.audio/cache/{other}"]

    # Later, Cover Art Archive itself is tried first again.
    third = CAA.replace("front-500", "front-1200")
    fake.on("GET", third, handler=_image())
    now = artwork.time.monotonic()
    monkeypatch.setattr(artwork.time, "monotonic", lambda: now + 601)
    fake.calls.clear()
    assert client.get("/v1/artwork", params={"src": third}).status_code == 200
    assert [str(c.url) for c in fake.calls] == [third]


def test_caa_404_is_final_the_mirrors_is_not(client, fake):
    fake.on("GET", CAA, status=404)
    assert client.get("/v1/artwork", params={"src": CAA}).status_code == 404
    assert fake.called("GET", MIRROR) == []                 # no art: the mirror isn't asked

    # Mirror first (after a failure) and the mirror says 404: Cover Art Archive decides.
    other = CAA.replace("front-500", "front-250")
    fake.on("GET", other, handler=_unreachable)
    fake.on("GET", f"https://images.lidarr.audio/cache/{other}", handler=_image())
    assert client.get("/v1/artwork", params={"src": other}).status_code == 200
    third = CAA.replace("front-500", "front-1200")
    fake.on("GET", f"https://images.lidarr.audio/cache/{third}", status=404)
    fake.on("GET", third, handler=_image(PNG, "image/png"))
    r = client.get("/v1/artwork", params={"src": third})
    assert r.status_code == 200 and r.content == PNG


def test_caa_and_mirror_both_down_is_a_retryable_502(client, fake):
    fake.on("GET", CAA, handler=_unreachable)
    fake.on("GET", MIRROR, handler=_image(b"<html>cloudflare error</html>", "text/html"))
    r = client.get("/v1/artwork", params={"src": CAA})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "artwork_unavailable" and r.json()["error"]["retryable"]
    assert not os.path.isdir(artwork.cache_dir()) or os.listdir(artwork.cache_dir()) == []
    assert not artwork.known_missing(artwork.cache_key(httpx.URL(CAA)))


def test_no_mirror_for_lidarr_images(client, fake):
    src = "https://images.lidarr.audio/cache/abc.jpg"
    fake.on("GET", src, handler=_unreachable)
    assert client.get("/v1/artwork", params={"src": src}).status_code == 502
    assert [str(c.url) for c in fake.calls] == [src]


def test_disk_cache_evicts_least_recently_used(fake, monkeypatch):
    _settings(monkeypatch, artwork_cache_mb=1)
    blob = JPEG + b"\x00" * 400_000
    d = artwork.cache_dir()
    artwork.cache_put("a", blob)
    artwork.cache_put("b", blob)
    os.utime(os.path.join(d, "a"), (1000, 1000))
    os.utime(os.path.join(d, "b"), (2000, 2000))
    assert artwork.cache_get("a") == blob                   # a is now the most recently used
    artwork.cache_put("c", blob)                            # 1.2 MB > 1 MB: evict down to 0.9
    assert sorted(os.listdir(d)) == ["a", "c"]
    artwork.cache_put("huge", JPEG + b"\x00" * (2 * 1024 * 1024))   # bigger than the cap
    assert "huge" not in os.listdir(d)


# --------------------------------------------------------------------------
# Rewriting in API outputs
# --------------------------------------------------------------------------
def _proxied(src):
    return {"url": f"https://music-api.test/v1/artwork?src={quote(src, safe='')}"}


def test_search_rewrites_catalogue_artwork(client, fake):
    make_user()
    s = login(client)
    fake.on("GET", "http://navidrome.test/rest/search3", {"subsonic-response": {
        "status": "ok", "searchResult3": {"album": [], "artist": [], "song": []}}})
    lidarr_img = "https://images.lidarr.audio/cache/geo.jpg"
    fake.on("GET", f"{LIDARR}/album/lookup", [
        {"foreignAlbumId": RG, "title": "Geogaddi", "artist": {"artistName": "BoC"},
         "images": [{"coverType": "cover", "remoteUrl": lidarr_img}]},
        {"foreignAlbumId": "4a1b2c3d-0000-4000-8000-000000000009", "title": "Other",
         "artist": {"artistName": "BoC"},
         "images": [{"coverType": "cover", "remoteUrl": "https://evil.example/x.jpg"}]},
    ])
    fake.on("GET", f"{LIDARR}/album", [])
    fake.on("GET", f"{LIDARR}/artist/lookup", [
        {"foreignArtistId": "art-boc", "artistName": "BoC",
         "images": [{"coverType": "poster", "remoteUrl": "https://images.lidarr.audio/boc.jpg"}]}])
    r = client.get("/v1/search", params={"q": "boc", "types": "album,artist"},
                   headers=bearer(s))
    results = {x["key"]: x for x in r.json()["results"]}
    assert results[f"mb:rg:{RG}"]["artwork"] == _proxied(lidarr_img)
    # A Lidarr image continuo can't proxy falls back to Cover Art Archive (through continuo).
    other = "4a1b2c3d-0000-4000-8000-000000000009"
    assert results[f"mb:rg:{other}"]["artwork"] == _proxied(
        f"https://coverartarchive.org/release-group/{other}/front-500")
    assert results["mb:artist:art-boc"]["artwork"] == _proxied("https://images.lidarr.audio/boc.jpg")
    # Nothing in the output points off-server.
    assert all(x["artwork"]["url"].startswith("https://music-api.test/") for x in results.values())


def test_unproxyable_artist_art_becomes_none():
    from continuo.search import merge
    out = merge.merge_artists([], [{"musicBrainzArtistId": "a", "name": "A",
                                    "imageUrl": "https://evil.example/a.jpg"}], 10)
    assert out[0]["artwork"] == {"url": None}


def test_catalog_artist_albums_rewrite_artwork(client, fake):
    make_user()
    s = login(client)
    art = "4a1b2c3d-0000-4000-8000-0000000000aa"
    fake.on("GET", f"{LIDARR}/artist", [])
    fake.on("GET", f"https://api.lidarr.audio/api/v0.4/artist/{art}", {
        "artistname": "BoC", "Albums": [{"Id": RG, "Title": "Geogaddi", "Type": "Album",
                                         "ReleaseStatuses": ["Official"]}]})
    r = client.get(f"/v1/catalog/artists/{art}/albums", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json()[0]["artwork"] == _proxied(CAA)


def test_request_output_rewrites_artwork(client, fake):
    make_user()
    s = login(client)
    row = req_db.create(mb_release_group_id=RG, title="Geogaddi", artist="BoC", year=2002,
                        artwork_url=CAA, user_id=1)
    r = client.get(f"/v1/requests/{row['id']}", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json()["artwork"] == _proxied(CAA)
    # The database keeps the source, so PUBLIC_BASE_URL can change without stale links.
    assert req_db.get(row["id"])["artwork_url"] == CAA
