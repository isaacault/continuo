"""The API shape of a request (PLAN.md §3.3)."""
from typing import Optional

from .. import config


def deep_link(navidrome_album_id: Optional[str]) -> Optional[str]:
    if not navidrome_album_id:
        return None
    return config.settings.deep_link_template.format(
        server=config.settings.public_host, albumId=navidrome_album_id)


def play(row: dict) -> Optional[dict]:
    if row.get("status") != "available" or not row.get("navidrome_album_id"):
        return None
    return {"navidromeAlbumId": row["navidrome_album_id"],
            "deepLink": deep_link(row["navidrome_album_id"])}


def to_out(row: dict, pipeline: Optional[dict] = None,
           requested_by: Optional[str] = None) -> dict:
    error = None
    if row.get("status") == "error":
        error = {"message": row.get("error") or "Couldn't add this album to Lidarr.",
                 "retryable": True}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row.get("title"),
        "artist": row.get("artist"),
        "year": row.get("year"),
        "artwork": {"url": row.get("artwork_url")},
        "ids": {
            "musicBrainzReleaseGroupId": row["mb_release_group_id"],
            "musicBrainzReleaseId": row.get("lidarr_release_id"),
            "lidarrArtistId": row.get("lidarr_artist_id"),
            "lidarrAlbumId": row.get("lidarr_album_id"),
            "navidromeAlbumId": row.get("navidrome_album_id"),
        },
        "status": row["status"],
        "stillLooking": bool(row.get("still_looking")),
        "pipeline": pipeline,
        "error": error,
        "note": row.get("note"),
        "requestedBy": requested_by,
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "availableAt": row.get("available_at"),
        "play": play(row),
    }


def slim(row: dict, pipeline: Optional[dict] = None) -> dict:
    """What request.updated SSE events carry."""
    return {
        "id": row["id"],
        "status": row["status"],
        "stillLooking": bool(row.get("still_looking")),
        "pipeline": pipeline,
        "play": play(row),
    }
