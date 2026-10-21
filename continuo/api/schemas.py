"""Request bodies. Responses are plain dicts shaped per PLAN.md §3 (camelCase)."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DeviceIn(BaseModel):
    name: str = Field("device", max_length=80)
    platform: Optional[str] = Field(None, max_length=32)


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    device: DeviceIn = DeviceIn()


class RefreshIn(BaseModel):
    refreshToken: str = Field(..., min_length=1, max_length=256)


class PasswordChange(BaseModel):
    currentPassword: str
    newPassword: str


class PushIn(BaseModel):
    kind: Literal["unifiedpush"]
    endpoint: str = Field(..., max_length=2048)


class RequestIn(BaseModel):
    kind: Literal["album", "track"] = "album"
    musicBrainzReleaseGroupId: str = Field(..., min_length=8, max_length=64)
    note: Optional[str] = Field(None, max_length=500)


class ReadIn(BaseModel):
    ids: Optional[list[int]] = None
    all: bool = False


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str
    role: Literal["admin", "user"] = "user"
    navidromeUsername: Optional[str] = Field(None, max_length=64)


class UserUpdate(BaseModel):
    role: Optional[Literal["admin", "user"]] = None
    navidromeUsername: Optional[str] = Field(None, max_length=64)
    password: Optional[str] = None


class SettingsIn(BaseModel):
    soularrQuality: Optional[Literal["mp3", "flac"]] = None
    features: Optional[dict[str, bool]] = None


class SearchEventIn(BaseModel):
    lidarrAlbumId: int
    at: Optional[str] = None
    rule: Optional[str] = Field(None, max_length=64)
