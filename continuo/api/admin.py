"""Admin: policy check, stats, users, settings (PLAN.md §3.5)."""
import logging

import httpx
from fastapi import APIRouter, Depends

from .. import config
from ..auth import facade, passwords
from ..auth.deps import CurrentUser, require_admin
from ..db import devices as devices_db
from ..db import library as lib_db
from ..db import requests as req_db
from ..db import settings as settings_db
from ..db import users as users_db
from ..errors import ApiError
from ..upstream import lidarr, navidrome, soularr
from ..workers import index
from .schemas import SettingsIn, UserCreate, UserUpdate

log = logging.getLogger("continuo")
router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _check(name: str, status: str, message: str, **extra) -> dict:
    return {"name": name, "status": status, "message": message, **extra}


async def policy_check() -> dict:
    """The constraints of PLAN.md §2.10 checked against the live services. status per check:
    ok | warn | unknown (couldn't be checked)."""
    s = config.settings
    checks = []

    try:
        qp = await lidarr.quality_profile(s.quality_profile_id)
        if qp.get("upgradeAllowed"):
            checks.append(_check("quality_profile_upgrades", "warn",
                                 f"Lidarr quality profile '{qp.get('name')}' allows upgrades — "
                                 "imported music could be replaced. Turn 'Upgrades Allowed' off."))
        else:
            checks.append(_check("quality_profile_upgrades", "ok",
                                 f"Quality profile '{qp.get('name')}' does not upgrade."))
    except httpx.HTTPError as e:
        checks.append(_check("quality_profile_upgrades", "unknown", f"Lidarr unreachable: {e}"))

    try:
        following = [a.get("artistName") for a in await lidarr.all_artists()
                     if a.get("monitorNewItems") not in (None, "none")]
        checks.append(_check(
            "artists_following_new_releases", "warn" if following else "ok",
            f"{len(following)} artist(s) auto-monitor new releases." if following
            else "No artist auto-monitors new releases.", artists=following[:50]))
    except httpx.HTTPError as e:
        checks.append(_check("artists_following_new_releases", "unknown", str(e)))

    try:
        meta = await lidarr.metadata_provider_config()
        write, scrub = meta.get("writeAudioTags"), meta.get("scrubAudioTags")
        ok = write in ("newFiles", "NewFiles") and not scrub
        checks.append(_check(
            "lidarr_tagging", "ok" if ok else "warn",
            f"Lidarr tagging: writeAudioTags={write}, scrubAudioTags={scrub}"
            + ("" if ok else " — want 'new files only' and scrub off (MBIDs in files, purchased "
               "music untouched)."), writeAudioTags=write, scrubAudioTags=scrub))
    except httpx.HTTPError as e:
        checks.append(_check("lidarr_tagging", "unknown", str(e)))

    try:
        hooks = [n for n in await lidarr.notifications() if n.get("implementation") == "Webhook"]
        ours = [n for n in hooks if any(
            f.get("name") == "url" and "/internal/webhooks/lidarr" in str(f.get("value") or "")
            for f in n.get("fields", []))]
        checks.append(_check("lidarr_webhook", "ok" if ours else "warn",
                             "Lidarr webhook to continuo is configured." if ours else
                             "No Lidarr webhook points at continuo (requests still complete via "
                             "polling, just slower)."))
    except httpx.HTTPError as e:
        checks.append(_check("lidarr_webhook", "unknown", str(e)))

    nd_ok = await navidrome.ping()
    mode = s.navidrome_auth
    checks.append(_check(
        "navidrome_auth", "ok" if nd_ok else "warn",
        f"Navidrome reachable with {mode} auth." if nd_ok else
        f"Navidrome unreachable or rejected continuo's {mode} auth"
        + (" — is continuo's address in ND_EXTAUTH_TRUSTEDSOURCES?" if mode == "header" else ".")))

    checks.append(_check("library_index", "ok" if lib_db.count() else "warn",
                         f"{lib_db.count()} albums indexed ({index.last_mode or 'not yet refreshed'})."))
    return {"ok": not any(c["status"] == "warn" for c in checks), "checks": checks}


@router.get("/policy-check")
async def get_policy_check(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return await policy_check()


@router.post("/library/refresh")
async def refresh_library(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return await index.refresh()


@router.get("/stats")
async def stats(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return {**req_db.stats(), "libraryAlbums": lib_db.count()}


def _user_out(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "navidromeUsername": u.get("navidrome_username") or u["username"],
            "createdAt": u["created_at"]}


@router.get("/users")
async def list_users(_admin: CurrentUser = Depends(require_admin)) -> list[dict]:
    return [_user_out(u) for u in users_db.list_all()]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, _admin: CurrentUser = Depends(require_admin)) -> dict:
    username = body.username.strip()
    if not username:
        raise ApiError(400, "bad_request", "Username is required.")
    passwords.validate(body.password)
    if users_db.get(username):
        raise ApiError(409, "username_taken", "That username is already taken.")
    u = users_db.create(username, passwords.hash_password(body.password), body.role,
                        body.navidromeUsername or None)
    return _user_out(u)


@router.patch("/users/{uid}")
async def update_user(uid: int, body: UserUpdate,
                      admin: CurrentUser = Depends(require_admin)) -> dict:
    u = users_db.get_by_id(uid)
    if not u:
        raise ApiError(404, "not_found", "User not found.")
    if body.role and body.role != u["role"]:
        if u["role"] == "admin" and users_db.count_admins() <= 1:
            raise ApiError(400, "last_admin", "Can't demote the last admin.")
        users_db.set_role(uid, body.role)
    if body.navidromeUsername is not None:
        users_db.set_navidrome_username(uid, body.navidromeUsername or None)
        facade.invalidate(u["username"])
    if body.password:
        passwords.validate(body.password)
        users_db.set_password(uid, passwords.hash_password(body.password))
        users_db.bump_token_version(uid)
    log.info("user %s updated by %s", u["username"], admin.username)
    return _user_out(users_db.get_by_id(uid))


@router.delete("/users/{uid}")
async def delete_user(uid: int, admin: CurrentUser = Depends(require_admin)) -> dict:
    target = users_db.get_by_id(uid)
    if not target:
        raise ApiError(404, "not_found", "User not found.")
    if uid == admin.id:
        raise ApiError(400, "cannot_delete_self", "You can't delete your own account.")
    if target["role"] == "admin" and users_db.count_admins() <= 1:
        raise ApiError(400, "last_admin", "Can't delete the last admin.")
    devices_db.revoke_all_for_user(uid)
    users_db.delete(uid)
    facade.invalidate_all()
    return {"ok": True, "message": f"Removed {target['username']}."}


@router.get("/settings")
async def get_settings(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return {"soularrQuality": soularr.get_quality(),
            "features": settings_db.get("features", {})}


@router.put("/settings")
async def put_settings(body: SettingsIn, _admin: CurrentUser = Depends(require_admin)) -> dict:
    message = "Saved."
    if body.features is not None:
        settings_db.put("features", body.features)
    if body.soularrQuality is not None:
        if not soularr.enabled():
            raise ApiError(409, "soularr_disabled",
                           "Quality control is only available with the Soularr adapter.")
        try:
            changed = soularr.set_quality(body.soularrQuality)
        except (RuntimeError, ValueError, OSError) as e:
            raise ApiError(503, "soularr_config", f"Couldn't update quality: {e}", True) from e
        if changed and not await soularr.try_trigger_run("put_settings"):
            message = (f"Quality set to {body.soularrQuality.upper()}; Soularr didn't restart, "
                       "it applies on its next cycle.")
        else:
            message = f"Quality set to {body.soularrQuality.upper()}."
    return {"ok": True, "message": message, **(await get_settings(_admin))}
