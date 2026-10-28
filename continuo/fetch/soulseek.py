"""One track from Soulseek through slskd (issue #24): search, pick the best copy, download it,
follow it. Lidarr only deals in whole albums, so single songs come straight from slskd.

Policies: `library` (a song added to the library) prefers formats in Soularr's own order
(`allowed_filetypes`, e.g. "flac,mp3 320"), then a free upload slot and a short queue;
`fastest` (Play now, which isn't kept in the library unless asked) takes any decent copy, lossless
or lossy at FAST_MIN_KBPS and up, whatever the library's formats, and puts the one that plays
soonest first: a free slot, a short queue, then the least time to reach the phone (to continuo at
the peer's speed, then on to the phone), then the smaller: between equally fast peers the smaller
copy wins at every quality (an MP3 over a FLAC, a 256 kbps over a 320)."""
import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx

from ..requests.normalise import normalise
from ..upstream import slskd, soularr

log = logging.getLogger("continuo")

LIBRARY, FASTEST = "library", "fastest"
_DEFAULT_FORMATS = [("flac", 0), ("mp3", 320)]
# Versions that aren't the song unless the title says so.
_VARIANTS = re.compile(r"\b(live|remix|instrumental|karaoke|acapella|a cappella|cover|demo|edit|sped up|slowed)\b")
_DURATION_SLACK_S = 8
# Play now: lossy copies below this aren't worth hearing; lossless always counts.
FAST_MIN_KBPS = 192
_LOSSLESS = {"flac", "wav", "aiff", "aif", "wv", "ape", "alac"}


class Offline(Exception):
    """slskd isn't connected to the Soulseek server (it lost the connection, or is reconnecting):
    nothing can be searched now. Not "nobody has it": callers try again later."""


@dataclass(frozen=True)
class Candidate:
    username: str
    filename: str          # the remote path, as slskd reports it
    size: int
    extension: str
    bit_rate: Optional[int]
    length_s: Optional[int]
    free_slot: bool
    queue_length: int
    upload_speed: int

    @property
    def parts(self) -> list[str]:
        return [p for p in re.split(r"[\\/]", self.filename) if p]

    @property
    def basename(self) -> str:
        return self.parts[-1] if self.parts else self.filename

    @property
    def folder(self) -> str:
        """slskd saves a download under its remote parent folder's name."""
        return self.parts[-2] if len(self.parts) > 1 else ""


def formats(policy: str = LIBRARY) -> list[tuple[str, int]]:
    """(extension, minimum kbps) in preference order, from Soularr's allowed_filetypes
    ("flac,mp3 320" -> flac, then mp3 at 320 kbps and up)."""
    raw = None
    try:
        raw = soularr._read().get("Search Settings", "allowed_filetypes", fallback=None) if soularr.enabled() else None
    except Exception:  # noqa: BLE001 — no Soularr config is the default, not an error
        raw = None
    out = []
    for part in (raw or "").split(","):
        bits = part.strip().lower().split()
        if bits:
            out.append((bits[0], int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 0))
    return out or list(_DEFAULT_FORMATS)


def _format_rank(c: Candidate, allowed: list[tuple[str, int]]) -> Optional[int]:
    for i, (ext, min_kbps) in enumerate(allowed):
        if c.extension == ext and (min_kbps == 0 or (c.bit_rate or 0) >= min_kbps):
            return i
    return None


def _decent(c: Candidate) -> bool:
    """Worth hearing: lossless, or lossy at FAST_MIN_KBPS and up (by its bit rate, else its size
    over its length; a copy saying neither counts)."""
    if c.extension in _LOSSLESS:
        return True
    if c.extension not in AUDIO:
        return False
    kbps = c.bit_rate or (c.size * 8 / 1000 / c.length_s if c.size and c.length_s else None)
    return kbps is None or kbps >= FAST_MIN_KBPS


# The second hop, continuo to the phone, as a rough rate (a phone on mobile data, or Wi-Fi in
# another room): what the peer's speed leaves out, and why a smaller copy wins between peers that
# are equally fast.
PHONE_BYTES_PER_S = 1_500_000


def download_s(c: Candidate) -> float:
    """Seconds this copy should take to reach the phone whole: from the peer to continuo (its
    size over the peer's upload speed), then on to the phone (its size over PHONE_BYTES_PER_S)."""
    if not c.size:
        return float("inf")
    return c.size / max(c.upload_speed, 1) + c.size / PHONE_BYTES_PER_S


def score(c: Candidate, artist: str, title: str, duration_ms: Optional[int], policy: str,
          allowed: list[tuple[str, int]]) -> Optional[tuple]:
    """Sort key for a copy (lower is better), or None when it isn't the song."""
    name, path = normalise(c.basename), normalise(c.filename)
    want, by = normalise(title), normalise(artist)
    if not want or want not in name or (by and by not in path):
        return None
    if policy == FASTEST:
        rank = 0 if _decent(c) else None
    else:
        rank = _format_rank(c, allowed)
    if rank is None:
        return None
    if duration_ms and c.length_s and abs(c.length_s - duration_ms / 1000) > _DURATION_SLACK_S:
        return None
    title_words = set(_VARIANTS.findall(title.lower()))
    variant = any(v not in title_words for v in _VARIANTS.findall(c.basename.lower()))
    if policy == FASTEST:
        return (variant, not c.free_slot, c.queue_length, download_s(c), c.size)
    return (variant, rank, not c.free_slot, c.queue_length, -c.upload_speed)


AUDIO = {"flac", "mp3", "m4a", "ogg", "opus", "wav", "aiff", "aif", "wv", "ape", "alac"}
IMAGES = {"jpg", "jpeg", "png"}


@dataclass(frozen=True)
class AlbumCandidate:
    """One user's folder holding the album: what to enqueue, and how good a bet it is."""
    username: str
    folder: str                          # the remote directory
    files: tuple[Candidate, ...]         # audio files, plus cover images
    matched: int
    free_slot: bool
    queue_length: int
    upload_speed: int

    @property
    def local_folder(self) -> str:
        """slskd saves a download under its remote parent folder's name."""
        return self.files[0].folder if self.files else ""


def _remote_dir(filename: str) -> str:
    parts = re.split(r"[\\/]", filename)
    return "\\".join(parts[:-1])


def best_album(responses: list[dict], album: str, track_titles: list[str],
               allowed: Optional[list[tuple[str, int]]] = None) -> Optional[AlbumCandidate]:
    """The best folder for a whole album: its name has the album title, it holds at least 80 %
    of the tracklist and every audio file is an acceptable format; then format preference, a
    free slot, a short queue and speed decide."""
    allowed = allowed if allowed is not None else formats(LIBRARY)
    want = [normalise(t) for t in track_titles if normalise(t)]
    title = normalise(album)
    folders: dict[tuple[str, str], list[Candidate]] = {}
    for c in candidates(responses):
        folders.setdefault((c.username, _remote_dir(c.filename)), []).append(c)
    best, best_key = None, None
    for (user, folder), files in folders.items():
        if not title or title not in normalise(re.split(r"[\\/]", folder)[-1]):
            continue
        audio = [f for f in files if f.extension in AUDIO]
        ranks = [_format_rank(f, allowed) for f in audio]
        if not audio or any(r is None for r in ranks):
            continue
        names = [normalise(f.basename) for f in audio]
        matched = sum(1 for t in want if any(t in n for n in names)) if want else len(audio)
        if want and (matched * 5 < len(want) * 4 or len(audio) > len(want) + 2):
            continue
        first = audio[0]
        key = (max(ranks), not first.free_slot, first.queue_length, -first.upload_speed, -matched)
        if best_key is None or key < best_key:
            covers = [f for f in files if f.extension in IMAGES]
            best = AlbumCandidate(user, folder, tuple(audio + covers), matched,
                                  first.free_slot, first.queue_length, first.upload_speed)
            best_key = key
    return best


async def enqueue_album(c: AlbumCandidate) -> None:
    r = await slskd._client().post(f"/transfers/downloads/{c.username}",
                                   json=[{"filename": f.filename, "size": f.size} for f in c.files])
    r.raise_for_status()


def candidates(responses: list[dict]) -> list[Candidate]:
    out = []
    for r in responses:
        for f in r.get("files") or []:
            fn = f.get("filename") or ""
            ext = (f.get("extension") or fn.rsplit(".", 1)[-1]).lower().lstrip(".")
            out.append(Candidate(
                username=r.get("username") or "", filename=fn, size=int(f.get("size") or 0), extension=ext,
                bit_rate=f.get("bitRate"), length_s=f.get("length"), free_slot=bool(r.get("hasFreeUploadSlot")),
                queue_length=int(r.get("queueLength") or 0), upload_speed=int(r.get("uploadSpeed") or 0),
            ))
    return out


def best(responses: list[dict], artist: str, title: str, duration_ms: Optional[int] = None,
         policy: str = LIBRARY, allowed: Optional[list[tuple[str, int]]] = None,
         variants: bool = True) -> Optional[Candidate]:
    """The best copy of a song; [variants]=False refuses live, remix, cover… copies outright
    (unless the title itself says so) instead of only ranking them last."""
    allowed = allowed if allowed is not None else formats(policy)
    scored = [(k, c) for c in candidates(responses) if (k := score(c, artist, title, duration_ms, policy, allowed))
              and (variants or not k[0])]
    return min(scored, key=lambda p: p[0])[1] if scored else None


# slskd ends a search after [quiet_s] without a new response, so a popular one runs well past
# it (45-50 s seen). Wait for it to finish, up to MAX_SEARCH_S; past that, stop it (its results
# stay) rather than delete it mid-flight, which loses every response and errors in slskd.
MAX_SEARCH_S = 120.0
POLL_S = 2.0


async def search(text: str, quiet_s: float = 15.0, max_s: float | None = None) -> list[dict]:
    """slskd search responses for [text]; [] when slskd is off or failed. [max_s] caps the wait
    (default MAX_SEARCH_S): Play now takes what it has after a few seconds. The search record is
    deleted afterwards (they pile up otherwise)."""
    if not slskd.enabled():
        return []
    c = slskd._client()
    sid = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    try:
        r = await c.post("/searches", json={"id": sid, "searchText": text, "searchTimeout": int(quiet_s * 1000)})
        r.raise_for_status()
        deadline = loop.time() + (MAX_SEARCH_S if max_s is None else max_s)
        stopped = False
        while True:
            s = await c.get(f"/searches/{sid}")
            s.raise_for_status()
            if s.json().get("isComplete"):
                break
            if loop.time() >= deadline:
                if stopped:
                    break                   # stopped and still not final: take what's there
                await c.put(f"/searches/{sid}")
                stopped, deadline = True, loop.time() + 10
            await asyncio.sleep(POLL_S)
        r = await c.get(f"/searches/{sid}/responses")
        r.raise_for_status()
        return r.json() or []
    except httpx.HTTPStatusError as e:
        # slskd answers 409 "must be connected and logged in" while it's off the server.
        if e.response.status_code == 409 and "connected" in e.response.text:
            raise Offline(e.response.text.strip('"')) from e
        log.warning("soulseek: search %r failed: %s", text, e)
        return []
    except (httpx.HTTPError, ValueError) as e:
        log.warning("soulseek: search %r failed: %s", text, e)
        return []
    finally:
        try:
            await c.delete(f"/searches/{sid}")
        except httpx.HTTPError:
            pass


_NOT_SEARCHABLE = re.compile(r"[^\w\s]+")


def _searchable(text: str) -> str:
    """Words only: Soulseek matches whole words, so "PRIDE." or "(feat. X)" can match nothing."""
    return " ".join(_NOT_SEARCHABLE.sub(" ", text or "").split())


def queries(artist: str, name: str) -> list[str]:
    """Search texts for a song or album, most specific first. Soulseek's server returns nothing
    for some artists' names as a search phrase ("Kendrick Lamar" gets no response at all, while
    "Kendrick DAMN" gets hundreds), so the alternatives leave out part of the name: the title
    with the artist's last word, with the first, then alone. Matching still wants the artist in
    a copy's path, so a wider search can't pick someone else's song."""
    a, n = _searchable(artist), _searchable(name)
    words = a.split()
    out = [f"{a} {n}".strip()]
    if len(words) > 1:
        out += [f"{n} {words[-1]}", f"{words[0]} {n}"]
    out.append(n)
    seen: set[str] = set()
    return [q for q in out if q and not (q.lower() in seen or seen.add(q.lower()))]


async def search_any(texts: list[str], quiet_s: float = 15.0, max_s: float | None = None) -> list[dict]:
    """The responses of the first of [texts] that gets any; [] when none does."""
    for text in texts:
        responses = await search(text, quiet_s=quiet_s, max_s=max_s)
        if responses:
            if text != texts[0]:
                log.info("soulseek: nothing for %r; %r found %d", texts[0], text, len(responses))
            return responses
    return []


async def enqueue(c: Candidate) -> None:
    r = await slskd._client().post(f"/transfers/downloads/{c.username}", json=[{"filename": c.filename, "size": c.size}])
    r.raise_for_status()


async def transfer(c: Candidate) -> Optional[dict]:
    """slskd's transfer for this copy ({state, percentComplete, ...}), or None if it's gone."""
    r = await slskd._client().get(f"/transfers/downloads/{c.username}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    for d in r.json().get("directories") or []:
        for f in d.get("files") or []:
            if f.get("filename") == c.filename:
                return f
    return None


def finished(t: Optional[dict]) -> Optional[bool]:
    """True when it completed, False when it failed for good, None while it's still going."""
    state = (t or {}).get("state") or ""
    if "Completed" not in state:
        return None
    return "Succeeded" in state
