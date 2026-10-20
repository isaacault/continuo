"""FastAPI dependencies: the current user (Bearer access JWT) and admin gating."""
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..db import devices as devices_db
from ..db import users as users_db
from ..errors import ApiError
from . import tokens

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str
    navidrome_username: str
    device_id: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def user_from_token(token: str) -> CurrentUser:
    payload = tokens.decode_access(token)
    # Re-validate against the DB on every request: a deleted user, a demoted role, a bumped
    # token_version (password change) or a revoked device ends the session now, not at expiry.
    user = users_db.get_by_id(payload.get("uid"))
    if not user or user["token_version"] != payload.get("ver", 0):
        raise ApiError(401, "session_expired", "Session expired — please sign in again.")
    device = devices_db.get(payload.get("did"))
    if not device or device["revoked_at"] or device["user_id"] != user["id"]:
        raise ApiError(401, "device_revoked", "This device was signed out.")
    return CurrentUser(user["id"], user["username"], user["role"],
                       users_db.navidrome_name(user), device["id"])


def require_user(request: Request,
                 creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> CurrentUser:
    if creds is None:
        raise ApiError(401, "missing_token", "Missing token.")
    user = user_from_token(creds.credentials)
    request.state.user = user
    return user


def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if not user.is_admin:
        raise ApiError(403, "admin_only", "Admin only.")
    return user


def can_see(user: CurrentUser, owner_id: int | None) -> bool:
    return user.is_admin or owner_id == user.id
