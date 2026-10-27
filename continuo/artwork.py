"""Catalogue artwork through continuo (issue #13).

Catalogue hits carry artwork on third-party hosts (Cover Art Archive, Lidarr's image cache).
Polyphony only talks to its own server, so API outputs point at `/v1/artwork?src=<url>` instead
and continuo fetches, checks and caches the image.

It must not become an open proxy:
- `src` must be https on a source host in SOURCE_HOSTS (no userinfo, no other port);
- redirects are followed by hand, at most MAX_REDIRECTS, and only to SOURCE_HOSTS or
  REDIRECT_DOMAINS (Cover Art Archive answers 307 → archive.org → iaNNN.us.archive.org);
  an http:// redirect on such a host is upgraded to https, never followed in the clear;
- only image bodies (content type and magic bytes both jpeg/png/webp/gif; never svg), at most
  ARTWORK_MAX_BYTES, unencoded (content-encoding refused, so no decompression bombs).

Cover Art Archive images all go through archive.org's front door, which goes dark now and then
(issue #15). For a well-formed CAA image URL, Lidarr's image cache (CAA_MIRROR) is the fallback:
`images.lidarr.audio/cache/<CAA URL>` serves the same bytes and sizes from Cloudflare and 404s
what CAA doesn't have. The mirror URL is built here, never taken from the client, and it goes
through the same checks.

The original upstream URL is what's stored (requests.artwork_url) and normalised (imageUrl);
the rewrite to continuo's URL happens only on output, so a changed PUBLIC_BASE_URL never leaves
stale links in the database.
"""
import hashlib
import logging
import os
import re
import tempfile
import time
from typing import Optional
from urllib.parse import quote

import httpx

from . import config
from .upstream import http

log = logging.getLogger("continuo")

# Hosts a `src` may name (what lidarr.coverart_url / _best_image produce).
SOURCE_HOSTS = frozenset({"coverartarchive.org", "images.lidarr.audio", "imagecache.lidarr.audio",
                          # Deezer's covers and artist pictures (search.ranked)
                          "cdn-images.dzcdn.net", "e-cdns-images.dzcdn.net"})
# Hosts (and their subdomains) only a redirect from a source host may lead to.
REDIRECT_DOMAINS = ("archive.org",)
MAX_REDIRECTS = 5

# Lidarr's cache of Cover Art Archive (what Lidarr's metadata server hands out for album art).
CAA_MIRROR = "https://images.lidarr.audio/cache/"
# After the direct CAA chain fails, try the mirror first for this long.
_DIRECT_DEGRADED_TTL = 600.0

_IMAGE_TYPES = {"image/jpeg": "image/jpeg", "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
                "image/png": "image/png", "image/webp": "image/webp", "image/gif": "image/gif"}

# Cover Art Archive serves 250/500/1200 px thumbnails of every image.
_CAA_SIZES = (250, 500, 1200)
_CAA_PATH = re.compile(
    r"^(/release(?:-group)?/[0-9a-fA-F-]{36}/(?:front|back|\d+))"
    r"(?:-(?:250|500|1200|small|large))?(\.jpg|\.png|\.gif)?$")

# Sources that answered 404 (an album without art is common): don't ask again for an hour.
_MISSING_TTL = 3600.0
_missing: dict[str, float] = {}
_degraded = {"until": 0.0}


class Refused(Exception):
    """The URL is not one continuo proxies (bad scheme, host, port…)."""


class NotFound(Exception):
    """The source has no image (upstream 404)."""


class Unusable(Exception):
    """Upstream answered with something continuo won't serve (not an image, too large, error)."""


# --------------------------------------------------------------------------
# URL checks and the public URL
# --------------------------------------------------------------------------
def _redirect_host_allowed(host: str) -> bool:
    return host in SOURCE_HOSTS or any(host == d or host.endswith("." + d)
                                       for d in REDIRECT_DOMAINS)


def check_source(src: str) -> httpx.URL:
    """The parsed `src`, or Refused."""
    try:
        url = httpx.URL(src)
    except (httpx.InvalidURL, TypeError, ValueError) as e:
        raise Refused("not a URL") from e
    if url.scheme != "https":
        raise Refused("only https sources")
    if url.userinfo or url.port not in (None, 443):
        raise Refused("no credentials or ports in the source")
    if url.host.lower() not in SOURCE_HOSTS:
        raise Refused("host not allowed")
    return url


def _redirect_target(current: httpx.URL, location: str) -> httpx.URL:
    try:
        url = current.join(location)
    except (httpx.InvalidURL, ValueError) as e:
        raise Refused("bad redirect") from e
    host = url.host.lower()
    if not _redirect_host_allowed(host):
        raise Refused(f"redirect to {host} not allowed")
    if url.userinfo:
        raise Refused("redirect carries credentials")
    if url.scheme == "http" and url.port in (None, 80):
        url = url.copy_with(scheme="https", port=None)
    if url.scheme != "https" or url.port not in (None, 443):
        raise Refused("redirect is not https")
    return url


def sized(url: httpx.URL, size: Optional[int]) -> httpx.URL:
    """Pick the Cover Art Archive thumbnail nearest above `size`; other sources as they are."""
    if not size or url.host.lower() != "coverartarchive.org":
        return url
    m = _CAA_PATH.match(url.path)
    if not m:
        return url
    px = next((s for s in _CAA_SIZES if s >= size), _CAA_SIZES[-1])
    return url.copy_with(path=f"{m.group(1)}-{px}{m.group(2) or ''}")


def mirror(url: httpx.URL) -> Optional[httpx.URL]:
    """Lidarr's cached copy of a Cover Art Archive image URL; None for anything else."""
    if (url.host.lower() != "coverartarchive.org" or url.query or url.fragment
            or not _CAA_PATH.match(url.path)):
        return None
    return httpx.URL(CAA_MIRROR + str(url))


def proxyable(src: Optional[str]) -> bool:
    if not src:
        return False
    try:
        check_source(src)
    except Refused:
        return False
    return True


def public_url(src: Optional[str]) -> Optional[str]:
    """continuo's URL for a third-party artwork URL; None when it isn't one continuo proxies
    (Polyphony drops third-party URLs anyway)."""
    if not proxyable(src):
        return None
    return f"{config.settings.public_base_url}/v1/artwork?src={quote(src, safe='')}"


def catalog_art(src: Optional[str]) -> dict:
    """The `artwork` object of a catalogue hit or request. Unauthenticated, unlike library art."""
    return {"url": public_url(src)}


# --------------------------------------------------------------------------
# Disk cache: one file per sized source URL, mtime = last use, oldest evicted over the cap
# --------------------------------------------------------------------------
_state: dict = {"dir": None, "total": None}


def cache_dir() -> str:
    s = config.settings
    return s.artwork_cache_dir or os.path.join(os.path.dirname(s.db_path) or ".", "artwork")


def cache_key(url: httpx.URL) -> str:
    return hashlib.sha256(str(url).encode()).hexdigest()


def _cap() -> int:
    return max(0, config.settings.artwork_cache_mb) * 1024 * 1024


def cache_get(key: str) -> Optional[bytes]:
    path = os.path.join(cache_dir(), key)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        os.utime(path)  # LRU-ish: mtime is the last use
    except OSError:
        pass
    return data if sniff(data) else None


def _scan(d: str) -> list[tuple[float, int, str]]:
    out = []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return out
    now = time.time()
    for e in entries:
        try:
            st = e.stat()
        except OSError:
            continue
        if e.name.startswith(".tmp-"):
            if now - st.st_mtime > 3600:  # left behind by a crash mid-write
                _unlink(e.path)
            continue
        out.append((st.st_mtime, st.st_size, e.path))
    return out


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _evict(d: str, cap: int) -> None:
    entries = sorted(_scan(d))
    total = sum(size for _, size, _ in entries)
    target = int(cap * 0.9)
    for _, size, path in entries:
        if total <= target:
            break
        _unlink(path)
        total -= size
    _state["total"] = total


def cache_put(key: str, data: bytes) -> None:
    cap = _cap()
    if not cap or len(data) > cap:
        return
    d = cache_dir()
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(d, key))
    except OSError as e:
        log.warning("artwork cache write failed: %s", e)
        return
    if _state["dir"] != d or _state["total"] is None:
        _state["dir"] = d
        _state["total"] = sum(size for _, size, _ in _scan(d))
    else:
        _state["total"] += len(data)
    if _state["total"] > cap:
        _evict(d, cap)


def reset() -> None:
    """Tests: forget the in-memory accounting and the 404 memory."""
    _state.update({"dir": None, "total": None})
    _missing.clear()
    _degraded["until"] = 0.0


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def sniff(data: bytes) -> Optional[str]:
    """The image type by magic bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _client() -> httpx.AsyncClient:
    # A short connect timeout: an unreachable host must leave time for the mirror before the
    # client (OkHttp: 10 s) gives up.
    return http.client("artwork", timeout=httpx.Timeout(8.0, connect=4.0), headers={
        "User-Agent": config.settings.musicbrainz_user_agent,
        "Accept": "image/*",
        "Accept-Encoding": "identity",
    })


def known_missing(key: str) -> bool:
    exp = _missing.get(key)
    if exp is None:
        return False
    if exp < time.monotonic():
        _missing.pop(key, None)
        return False
    return True


def remember_missing(key: str) -> None:
    if len(_missing) > 4096:
        _missing.clear()
    _missing[key] = time.monotonic() + _MISSING_TTL


async def fetch(url: httpx.URL) -> tuple[bytes, str]:
    """(body, content type) of an allow-listed image, following only allow-listed redirects."""
    limit = config.settings.artwork_max_bytes
    c = _client()
    for _ in range(MAX_REDIRECTS + 1):
        async with c.stream("GET", url) as r:
            if r.is_redirect:
                url = _redirect_target(url, r.headers["location"])
                continue
            if r.status_code in (404, 410):
                raise NotFound()
            if r.status_code != 200:
                raise Unusable(f"upstream answered {r.status_code}")
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in _IMAGE_TYPES:
                raise Unusable(f"not an image ({ctype or 'no content type'})")
            if r.headers.get("content-encoding", "identity").lower() != "identity":
                raise Unusable("encoded body")
            length = r.headers.get("content-length", "")
            if length.isdigit() and int(length) > limit:
                raise Unusable("too large")
            buf = bytearray()
            async for chunk in r.aiter_bytes():  # identity only (checked above): no decoding
                buf += chunk
                if len(buf) > limit:
                    raise Unusable("too large")
            data = bytes(buf)
            kind = sniff(data)
            if kind is None:
                raise Unusable("body is not an image")
            return data, kind
    raise Refused("too many redirects")


async def fetch_image(url: httpx.URL) -> tuple[bytes, str]:
    """fetch(), falling back to Lidarr's mirror for Cover Art Archive images. CAA's own 404 is
    final, the mirror's isn't. When both fail, the direct attempt's error is raised. After a
    direct failure the mirror goes first for _DIRECT_DEGRADED_TTL, so an archive.org outage
    doesn't cost every cover a connect timeout."""
    alt = mirror(url)
    if alt is None:
        return await fetch(url)
    degraded = _degraded["until"] > time.monotonic()
    direct_error: Optional[Exception] = None
    for u in ((alt, url) if degraded else (url, alt)):
        try:
            result = await fetch(u)
        except NotFound:
            if u is url:
                raise
            continue
        except (Refused, Unusable, httpx.HTTPError) as e:
            if u is url:
                direct_error = e
                if not degraded:
                    log.warning("artwork: Cover Art Archive failed (%s: %s), using Lidarr's "
                                "mirror first for %d min", type(e).__name__, e,
                                _DIRECT_DEGRADED_TTL // 60)
                _degraded["until"] = time.monotonic() + _DIRECT_DEGRADED_TTL
            continue
        if u is url:
            _degraded["until"] = 0.0
        return result
    assert direct_error is not None  # the direct attempt either returned or raised NotFound
    raise direct_error
