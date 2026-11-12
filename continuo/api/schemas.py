"""Request bodies. Responses are plain dicts shaped per PLAN.md §3 (camelCase)."""
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, Field


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


# A label from the phone: taken whatever it says (a value a newer Polyphony adds included), cut short.
_Label = Annotated[str, AfterValidator(lambda v: v[:64])]


class ConnectionSampleIn(BaseModel):
    """Polyphony's ConnectionSample (polyphony#185): about 30 s of this phone streaming. Every
    field is sent, the nullable ones as null. Enums are plain strings: a sample is never refused
    for a value continuo doesn't know yet, since a refused batch would be sent again forever."""
    startedAtMs: int
    endedAtMs: int
    trigger: _Label                     # PERIODIC | NETWORK_CHANGE | STOPPED
    network: _Label                     # WIFI | ETHERNET | CELLULAR | OTHER | NONE
    metered: bool
    vpn: bool
    atHome: Optional[bool]              # null: couldn't tell
    responseMs: Optional[int]           # median time to first response
    networkBytes: int
    networkMs: int
    throughputKbps: Optional[int]       # null: too little read to tell
    cachedBytes: int
    downloadedBytes: int
    stalls: int
    stallMs: int
    serverId: Optional[_Label]
    profileId: Optional[_Label]         # adaptive.continuo: HLS
    codec: Optional[_Label]
    maxBitrateKbps: Optional[int]       # HLS: the level playing
    reason: Optional[_Label]
    capped: bool
    cappedMs: int


class ConnectionIn(BaseModel):
    """Oldest first, at most 200 a batch."""
    samples: list[ConnectionSampleIn] = Field(..., max_length=200)


class RequestIn(BaseModel):
    kind: Literal["album", "track"] = "album"
    musicBrainzReleaseGroupId: str = Field(..., min_length=8, max_length=64)
    # kind=track: the song (its album is musicBrainzReleaseGroupId).
    musicBrainzRecordingId: Optional[str] = Field(None, min_length=8, max_length=64)
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
