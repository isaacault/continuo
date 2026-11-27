"""Access JWTs (short-lived, HS256) and rotating per-device refresh tokens.

Login creates a device: refresh token (stored as sha256) + façade secret. Refresh rotates the
token; presenting the previous token again means it leaked, so the whole device is revoked —
unless it comes back within REFRESH_REUSE_GRACE_SECONDS of the rotation, which is a retry of a
refresh whose reply was lost (#129): that rotates again instead."""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .. import config
from ..db import devices as devices_db
from ..errors import ApiError
from . import facade

log = logging.getLogger("continuo")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_refresh_token() -> str:
    return "crt_" + secrets.token_urlsafe(32)


def new_facade_secret() -> str:
    return secrets.token_urlsafe(24)


def new_device_id() -> str:
    return "dev_" + secrets.token_hex(6)


def issue_access(user: dict, device_id: str) -> tuple[str, int]:
    ttl = config.settings.access_token_ttl
    now = _now()
    payload = {
        "typ": "access",
        "sub": user["username"],
        "uid": user["id"],
        "did": device_id,
        "ver": user.get("token_version", 0),
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, config.settings.jwt_secret, algorithm="HS256"), ttl


def decode_access(token: str) -> dict:
    try:
        payload = jwt.decode(token, config.settings.jwt_secret, algorithms=["HS256"],
                             options={"require": ["exp", "uid", "did"]})
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "Invalid or expired token.") from None
    if payload.get("typ") != "access":
        raise ApiError(401, "invalid_token", "Invalid or expired token.")
    return payload


def _refresh_expiry() -> str:
    return (_now() + timedelta(days=config.settings.refresh_token_ttl_days)).isoformat(
        timespec="seconds")


def create_device(user: dict, name: str, platform: str | None) -> tuple[dict, str]:
    """A new device for this login. Returns (device row, plaintext refresh token)."""
    refresh = new_refresh_token()
    device = devices_db.create(
        device_id=new_device_id(), user_id=user["id"], name=name[:80] or "device",
        platform=(platform or None) and platform[:32], refresh_hash=_sha256(refresh),
        refresh_expires_at=_refresh_expiry(), facade_secret=new_facade_secret(),
    )
    facade.invalidate(user["username"])
    return device, refresh


def _within_grace(device: dict) -> bool:
    """Was the device's token rotated so recently that its predecessor coming back is a retry?"""
    rotated = device.get("refresh_rotated_at")
    if not rotated:
        return False
    age = (_now() - datetime.fromisoformat(rotated)).total_seconds()
    return 0 <= age <= config.settings.refresh_reuse_grace_seconds


def rotate(refresh_token: str) -> tuple[dict, str]:
    """Validate a refresh token and rotate it. Returns (device, new refresh token)."""
    presented = _sha256(refresh_token or "")
    device, reused = devices_db.by_refresh_hash(presented)
    if device is None:
        raise ApiError(401, "invalid_refresh_token", "Please sign in again.")
    if reused and _within_grace(device):
        # #129: the previous token, moments after it was rotated. The reply to that refresh was
        # lost (dropped connection, timeout, an HTTP client replaying the POST) and the device is
        # retrying with the only token it has. Rotate again; the token in the lost reply dies here.
        log.info("refresh: device %s (%s) retried a refresh within %ss; rotating again",
                 device["id"], device["name"], config.settings.refresh_reuse_grace_seconds)
    elif reused:
        # An already-rotated token came back long after: whoever holds it is not (only) the device.
        devices_db.revoke(device["id"])
        facade.invalidate_all()
        log.warning("refresh: device %s (%s) of user %s revoked: a refresh token rotated at %s "
                    "came back (refresh_token_reused)", device["id"], device["name"],
                    device["user_id"], device.get("refresh_rotated_at") or "an unknown time")
        raise ApiError(401, "refresh_token_reused",
                       "This sign-in was revoked for safety. Please sign in again.")
    if device["revoked_at"]:
        raise ApiError(401, "device_revoked", "This device was signed out.")
    expires = device.get("refresh_expires_at")
    if expires and datetime.fromisoformat(expires) < _now():
        raise ApiError(401, "refresh_token_expired", "Please sign in again.")
    new = new_refresh_token()
    rotated_at = _now().isoformat(timespec="seconds")
    if not devices_db.rotate_refresh(device["id"], _sha256(new), _refresh_expiry(), rotated_at,
                                     presented):
        # Another refresh of this device committed first (two copies of the same request at once).
        raise ApiError(409, "refresh_in_progress", "Another refresh just ran; try again.",
                       retryable=True)
    return devices_db.get(device["id"]), new
