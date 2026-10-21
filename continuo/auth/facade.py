"""The Subsonic façade check behind Caddy's forward_auth.

A client talks Subsonic to music-api.ault.me with u=<continuo user> and its per-device façade
secret as the password (token form t=md5(secret+s), or p= / p=enc:<hex>). Caddy asks us; we
answer 200 + Remote-User: <navidrome user>, and Caddy proxies to Navidrome, which trusts that
header from Caddy only. Audio never passes through continuo.

Hot path (every stream, cover and scrobble): secrets are cached per user for a few seconds and
the cache is dropped on any revocation, so a revoked device fails on its very next request.
"""
import hashlib
import hmac
import time
from dataclasses import dataclass

from ..db import devices as devices_db
from ..db import users as users_db

_TTL = 30.0
_cache: dict[str, tuple[float, str | None, list[tuple[str, str]]]] = {}
_seen: dict[str, float] = {}
_TOUCH_EVERY = 300.0


@dataclass(frozen=True)
class FacadeIdentity:
    username: str
    navidrome_username: str
    device_id: str


def invalidate(username: str | None = None) -> None:
    if username is None:
        _cache.clear()
    else:
        _cache.pop(username, None)


def invalidate_all() -> None:
    _cache.clear()


def _lookup(username: str) -> tuple[str | None, list[tuple[str, str]]]:
    now = time.monotonic()
    hit = _cache.get(username)
    if hit and now - hit[0] < _TTL:
        return hit[1], hit[2]
    user = users_db.get(username)
    if user is None:
        nd_name, secrets = None, []
    else:
        nd_name, secrets = users_db.navidrome_name(user), devices_db.active_secrets_for_username(username)
    if len(_cache) > 1000:
        _cache.clear()
    _cache[username] = (now, nd_name, secrets)
    return nd_name, secrets


def _password_from_p(p: str) -> str:
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode()
        except ValueError:
            return ""
    return p


def verify(params: dict) -> FacadeIdentity | None:
    """Check Subsonic credentials from a request's query parameters."""
    username = params.get("u") or ""
    if not username:
        return None
    nd_name, secrets = _lookup(username)
    if not nd_name or not secrets:
        return None
    token, salt, password = params.get("t"), params.get("s"), params.get("p")
    matched = None
    if token and salt:
        token = token.lower()
        for device_id, secret in secrets:
            expected = hashlib.md5((secret + salt).encode()).hexdigest()
            if hmac.compare_digest(expected, token):
                matched = device_id
                break
    elif password:
        given = _password_from_p(password)
        for device_id, secret in secrets:
            if hmac.compare_digest(secret.encode(), given.encode()):
                matched = device_id
                break
    if matched is None:
        return None
    _touch(matched)
    return FacadeIdentity(username, nd_name, matched)


def _touch(device_id: str) -> None:
    """Record last-seen, at most every few minutes (this runs for every audio request)."""
    now = time.monotonic()
    if now - _seen.get(device_id, 0.0) < _TOUCH_EVERY:
        return
    _seen[device_id] = now
    devices_db.touch(device_id)
