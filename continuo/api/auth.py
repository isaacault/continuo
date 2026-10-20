"""Login, refresh, logout, me, devices (PLAN.md §3.1)."""
import logging

from fastapi import APIRouter, Depends, Request

from .. import config
from ..auth import facade, passwords, tokens
from ..auth.deps import CurrentUser, require_user
from ..db import devices as devices_db
from ..db import users as users_db
from ..errors import ApiError
from ..ratelimit import client_ip, login_limiter
from .schemas import LoginIn, PasswordChange, PushIn, RefreshIn

log = logging.getLogger("continuo")
router = APIRouter(prefix="/v1", tags=["auth"])

CAPABILITIES = ["requests", "notifications", "events", "search"]


def _user_out(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def _device_out(d: dict, current_id: str | None = None) -> dict:
    return {"id": d["id"], "name": d["name"], "platform": d["platform"],
            "createdAt": d["created_at"], "lastSeenAt": d["last_seen_at"],
            "current": d["id"] == current_id}


def library_credential(user: dict, device: dict) -> dict:
    """What a Subsonic client needs — Polyphony adds it as a Server."""
    return {"type": "subsonic", "baseUrl": config.settings.public_base_url,
            "username": user["username"], "password": device["facade_secret"]}


def _session(user: dict, device: dict, refresh: str) -> dict:
    access, ttl = tokens.issue_access(user, device["id"])
    return {"accessToken": access, "expiresIn": ttl, "refreshToken": refresh,
            "user": _user_out(user), "device": {"id": device["id"], "name": device["name"]},
            "library": library_credential(user, device)}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request) -> dict:
    ip = client_ip(request)
    if login_limiter.is_blocked(ip):
        raise ApiError(429, "rate_limited", "Too many failed sign-ins. Try again in a few minutes.",
                       True)
    user = users_db.get(body.username)
    if user is None:
        passwords.burn_time(body.password)
    if user is None or not passwords.check_password(body.password, user["password_hash"]):
        login_limiter.register(ip)
        log.info("failed login for %r from %s", body.username, ip)
        raise ApiError(401, "invalid_credentials", "Invalid username or password.")
    login_limiter.clear(ip)
    device, refresh = tokens.create_device(user, body.device.name, body.device.platform)
    log.info("login: %s on new device %s (%s)", user["username"], device["id"], device["name"])
    return _session(user, device, refresh)


@router.post("/auth/refresh")
async def refresh(body: RefreshIn) -> dict:
    device, new_refresh = tokens.rotate(body.refreshToken)
    user = users_db.get_by_id(device["user_id"])
    if user is None:
        raise ApiError(401, "session_expired", "Please sign in again.")
    return _session(user, device, new_refresh)


@router.post("/auth/logout")
async def logout(user: CurrentUser = Depends(require_user)) -> dict:
    devices_db.revoke(user.device_id)
    facade.invalidate(user.username)
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser = Depends(require_user)) -> dict:
    device = devices_db.get(user.device_id)
    return {"user": {"id": user.id, "username": user.username, "role": user.role},
            "device": _device_out(device, user.device_id),
            "capabilities": CAPABILITIES}


@router.get("/me/library")
async def my_library(user: CurrentUser = Depends(require_user)) -> dict:
    """This device's Subsonic credential again (e.g. to re-add the server in the app)."""
    return library_credential(users_db.get_by_id(user.id), devices_db.get(user.device_id))


@router.put("/me/password")
async def change_password(body: PasswordChange, user: CurrentUser = Depends(require_user)) -> dict:
    row = users_db.get_by_id(user.id)
    if not passwords.check_password(body.currentPassword, row["password_hash"]):
        raise ApiError(400, "wrong_password", "Current password is incorrect.")
    passwords.validate(body.newPassword)
    users_db.set_password(user.id, passwords.hash_password(body.newPassword))
    # Other devices keep playback (their façade secrets are random, not the password) but must
    # refresh: bump the token version so every access token re-validates.
    users_db.bump_token_version(user.id)
    return {"ok": True, "message": "Password changed. Other devices will need to refresh."}


@router.get("/devices")
async def list_devices(user: CurrentUser = Depends(require_user)) -> list[dict]:
    return [_device_out(d, user.device_id) for d in devices_db.list_for_user(user.id)]


@router.delete("/devices/{device_id}")
async def revoke_device(device_id: str, user: CurrentUser = Depends(require_user)) -> dict:
    d = devices_db.get(device_id)
    if d is None or (d["user_id"] != user.id and not user.is_admin):
        raise ApiError(404, "not_found", "Device not found.")
    devices_db.revoke(device_id)
    facade.invalidate_all()
    log.info("device %s revoked by %s", device_id, user.username)
    return {"ok": True}


@router.post("/devices/revoke-others")
async def revoke_others(user: CurrentUser = Depends(require_user)) -> dict:
    n = 0
    for d in devices_db.list_for_user(user.id):
        if d["id"] != user.device_id and devices_db.revoke(d["id"]):
            n += 1
    facade.invalidate(user.username)
    return {"ok": True, "revoked": n}


@router.put("/devices/{device_id}/push")
async def set_push(device_id: str, body: PushIn, user: CurrentUser = Depends(require_user)) -> dict:
    d = devices_db.get(device_id)
    if d is None or d["user_id"] != user.id:
        raise ApiError(404, "not_found", "Device not found.")
    devices_db.set_push(device_id, body.kind, body.endpoint)
    return {"ok": True}
