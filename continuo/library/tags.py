"""Writing artist tags on music continuo had imported for it (never on files that were already in
the library). Used to file an album Lidarr imports under the library's own name for its artist
(Ichiko Aoba rather than 青葉市子), so Navidrome shows one artist."""
import logging
import os

from .. import config
from ..requests.normalise import normalise

log = logging.getLogger("continuo")

AUDIO = (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".wv", ".ape")


def library_path(lidarr_path: str, music_dir: str) -> str | None:
    """A path as Lidarr reports it (under ROOT_FOLDER_PATH) as continuo sees it (under
    MUSIC_DIR); None when it isn't under Lidarr's root."""
    root = config.settings.root_folder_path.rstrip("/")
    p = lidarr_path.replace("\\", "/")
    if not p.startswith(root + "/"):
        return None
    out = os.path.realpath(os.path.join(music_dir, p[len(root) + 1:]))
    return out if out.startswith(music_dir + os.sep) else None


def rename_artist(paths: list[str], from_names: set[str], to: str, sort: str | None = None) -> int:
    """Replace artist / album artist values equal to one of [from_names] (normalised) with [to],
    and set the matching sort tag to [sort] when given. Returns how many files changed.
    Collaborations ("A & B") are left alone."""
    import mutagen
    keys = {normalise(n) for n in from_names}
    changed = 0
    for path in paths:
        try:
            f = mutagen.File(path, easy=True)
        except Exception as e:  # noqa: BLE001 — a damaged file stays as it is
            log.warning("tags: can't read %s: %s", path, e)
            continue
        if f is None or f.tags is None:
            continue
        dirty = False
        for field in ("artist", "albumartist"):
            values = list(f.tags.get(field) or [])
            new = [to if normalise(v) in keys else v for v in values]
            if new != values:
                f.tags[field] = new
                dirty = True
                if sort:
                    try:
                        f.tags[field + "sort"] = [sort]
                    except (KeyError, ValueError):  # a format without that tag in mutagen's easy names
                        pass
        if dirty:
            f.save()
            changed += 1
    return changed


def audio_files(folder: str) -> list[str]:
    out = []
    for root, _dirs, files in os.walk(folder):
        out += [os.path.join(root, f) for f in files if f.lower().endswith(AUDIO)]
    return sorted(out)
