"""Access JWTs (short-lived, HS256) and rotating per-device refresh tokens.

Login creates a device: refresh token (stored as sha256) + façade secret. Refresh rotates the
token; presenting the previous token again means it leaked, so the whole device is revoked."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .. import config
from ..db import devices as devices_db
from ..errors import ApiError
from . import facade


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


def rotate(refresh_token: str) -> tuple[dict, str]:
    """Validate a refresh token and rotate it. Returns (device, new refresh token)."""
    device, reused = devices_db.by_refresh_hash(_sha256(refresh_token or ""))
    if device is None:
        raise ApiError(401, "invalid_refresh_token", "Please sign in again.")
    if reused:
        # An already-rotated token came back: whoever holds it is not (only) the device.
        devices_db.revoke(device["id"])
        facade.invalidate_all()
        raise ApiError(401, "refresh_token_reused",
                       "This sign-in was revoked for safety. Please sign in again.")
    if device["revoked_at"]:
        raise ApiError(401, "device_revoked", "This device was signed out.")
    expires = device.get("refresh_expires_at")
    if expires and datetime.fromisoformat(expires) < _now():
        raise ApiError(401, "refresh_token_expired", "Please sign in again.")
    new = new_refresh_token()
    devices_db.rotate_refresh(device["id"], _sha256(new), _refresh_expiry())
    return devices_db.get(device["id"]), new
