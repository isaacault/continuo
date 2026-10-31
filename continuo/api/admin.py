"""Admin: policy check, stats, users, settings (PLAN.md §3.5)."""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from ..auth import facade, passwords
from ..auth.deps import CurrentUser, require_admin
from ..db import devices as devices_db
from ..db import requests as req_db
from ..db import settings as settings_db
from ..db import users as users_db
from ..db.conn import parse_ts
from ..errors import ApiError
from .schemas import SettingsIn, UserCreate, UserUpdate

log = logging.getLogger("continuo")
router = APIRouter(prefix="/v1/admin", tags=["admin"])


_LATENCY_STEPS = (
    ("toDownloadStart", "created_at", "download_started_at"),
    ("toFirstFile", "created_at", "first_file_at"),
    ("toAvailable", "created_at", "available_at"),
    ("downloadToFirstFile", "download_started_at", "first_file_at"),
)


def _seconds(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None or tb is None:
        return None
    return max(0.0, (tb - ta).total_seconds())


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def latency_report(rows: list[dict]) -> dict:
    """Median and p90 seconds per source for each step from request to playable (issue #10)."""
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r.get("first_source") or r.get("last_source") or "unknown", []).append(r)
    by_source["all"] = rows

    def summary(group: list[dict]) -> dict:
        out = {"requests": len(group)}
        for name, start, end in _LATENCY_STEPS:
            values = [v for v in (_seconds(r.get(start), r.get(end)) for r in group) if v is not None]
            out[name] = ({"n": len(values), "median": _percentile(values, 0.5),
                          "p90": _percentile(values, 0.9)} if values else {"n": 0})
        return out

    return {name: summary(group) for name, group in by_source.items()}


@router.get("/latency")
async def latency(days: int = Query(30, ge=1, le=365),
                  _admin: CurrentUser = Depends(require_admin)) -> dict:
    """How long requests take to become playable, per source — the evidence for designing
    "play now" (issue #10). Seconds; first file = first complete audio file anywhere."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = req_db.latency_rows(since)
    recent = [{
        "id": r["id"], "title": r["title"], "artist": r["artist"], "status": r["status"],
        "source": r.get("first_source") or r.get("last_source"),
        **{name: _seconds(r.get(start), r.get(end)) for name, start, end in _LATENCY_STEPS},
    } for r in rows[:50]]
    return {"days": days, "bySource": latency_report(rows), "recent": recent}


@router.get("/stats")
async def stats(_admin: CurrentUser = Depends(require_admin)) -> dict:
    from .. import modules
    out = req_db.stats()
    if modules.is_on("music"):
        from ..db import library as lib_db
        out["libraryAlbums"] = lib_db.count()
    return out


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
    from .. import modules
    out = {"features": settings_db.get("features", {})}
    if modules.is_on("music"):
        from ..upstream import soularr
        out = {"soularrQuality": soularr.get_quality(), **out}
    return out


@router.put("/settings")
async def put_settings(body: SettingsIn, _admin: CurrentUser = Depends(require_admin)) -> dict:
    message = "Saved."
    if body.features is not None:
        settings_db.put("features", body.features)
    if body.soularrQuality is not None:
        from .. import modules
        from ..upstream import soularr
        if not modules.is_on("music") or not soularr.enabled():
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
