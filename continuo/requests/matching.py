"""Lidarr album ⇄ Navidrome album. Deterministic signals first, a scored heuristic last, and an
ambiguous result is never silently linked: it becomes needs_attention with the candidates.

1. paths       Lidarr's imported file paths ↔ the Navidrome album's folder (no tags needed)
2. release_group  Navidrome mbzReleaseGroupId == Lidarr foreignAlbumId
3. release     Navidrome mbzAlbumId ∈ Lidarr releases[].foreignReleaseId
4. fuzzy       normalised artist + title, year ±1, track count
"""
import posixpath
from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..upstream.navidrome import relative_path
from .normalise import album_key, normalise, strip_edition

ACCEPT_SCORE = 0.8
CANDIDATE_SCORE = 0.6


@dataclass
class Match:
    strategy: Optional[str] = None
    navidrome_album_id: Optional[str] = None
    ambiguous: bool = False
    candidates: list[dict] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.navidrome_album_id is not None


def _candidate(a: dict, score: float | None = None) -> dict:
    out = {"navidromeAlbumId": a["navidrome_id"], "name": a["name"], "artist": a.get("artist"),
           "year": a.get("year"), "songCount": a.get("song_count"), "folder": a.get("folder")}
    if score is not None:
        out["score"] = round(score, 2)
    return out


def import_dirs(paths: list[str]) -> set[str]:
    """Album directories (relative to Lidarr's root folder) of imported track files."""
    root = config.settings.root_folder_path
    return {posixpath.dirname(relative_path(p, root)) for p in paths if p} - {""}


def _same_title(a: dict, title: Optional[str]) -> bool:
    return normalise(strip_edition(a.get("name"))) == normalise(strip_edition(title))


def _pick(cands: list[dict], row: dict, release_id: Optional[str]) -> Optional[dict]:
    """Narrow several deterministic candidates to one, or None if it stays ambiguous."""
    uniq = {a["navidrome_id"]: a for a in cands}
    if len(uniq) == 1:
        return next(iter(uniq.values()))
    for narrow in (
        lambda a: release_id is not None and a.get("mbz_album_id") == release_id,
        lambda a: _same_title(a, row.get("title")),
    ):
        hits = [a for a in uniq.values() if narrow(a)]
        if len(hits) == 1:
            return hits[0]
    return None


def by_paths(dirs: set[str], library: list[dict]) -> list[dict]:
    """Library albums whose folder is an import dir, or one level above/below one (multi-disc
    layouts: Album/CD1 + Album/CD2). Never further: a loose-files artist folder must not match
    every album of that artist."""
    if not dirs:
        return []
    parents = {posixpath.dirname(d) for d in dirs} - {""}
    out = []
    for a in library:
        folder = a.get("folder")
        if not folder:
            continue
        if folder in dirs or (folder in parents and len(dirs) > 1) \
                or posixpath.dirname(folder) in dirs:
            out.append(a)
    return out


def fuzzy_score(a: dict, row: dict, track_count: Optional[int]) -> float:
    if album_key(a.get("artist"), a.get("name")) != album_key(row.get("artist"), row.get("title")):
        return 0.0
    score = 0.6
    y, ry = a.get("year"), row.get("year")
    if y and ry and abs(y - ry) <= 1:
        score += 0.2
    n = a.get("song_count")
    if n and track_count:
        if n == track_count:
            score += 0.2
        elif abs(n - track_count) <= 1:
            score += 0.1
    return score


def match(row: dict, library: list[dict], *, lidarr_album: Optional[dict] = None,
          paths: Optional[list[str]] = None) -> Match:
    rg = row.get("mb_release_group_id")
    releases = (lidarr_album or {}).get("releases") or []
    release_ids = [r.get("foreignReleaseId") for r in releases if r.get("foreignReleaseId")]
    monitored = row.get("lidarr_release_id") or next(
        (r.get("foreignReleaseId") for r in releases if r.get("monitored")), None)
    stats = (lidarr_album or {}).get("statistics") or {}
    track_count = stats.get("trackCount")

    dirs = import_dirs(paths or row.get("import_paths") or [])
    cands = by_paths(dirs, library)
    if cands:
        picked = _pick(cands, row, monitored)
        if picked:
            return Match("paths", picked["navidrome_id"], detail={"dirs": sorted(dirs)})

    if rg:
        cands = [a for a in library if a.get("mbz_release_group_id") == rg]
        picked = _pick(cands, row, monitored) if cands else None
        if picked:
            return Match("release_group", picked["navidrome_id"])

    if release_ids:
        wanted = set(release_ids)
        cands = [a for a in library if a.get("mbz_album_id") in wanted]
        picked = _pick(cands, row, monitored) if cands else None
        if picked:
            return Match("release", picked["navidrome_id"])

    scored = sorted(((fuzzy_score(a, row, track_count), a) for a in library),
                    key=lambda x: x[0], reverse=True)
    scored = [(s, a) for s, a in scored if s >= CANDIDATE_SCORE]
    if scored:
        top = scored[0][0]
        tied = [a for s, a in scored if s == top]
        if top >= ACCEPT_SCORE and len(tied) == 1:
            return Match("fuzzy", tied[0]["navidrome_id"], detail={"score": round(top, 2)})
        return Match("fuzzy", None, ambiguous=True,
                     candidates=[_candidate(a, s) for s, a in scored[:5]])
    return Match()
