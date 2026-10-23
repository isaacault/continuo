"""Latin names for artists named in another script (NAME_LOCALE): 青葉市子 is "Ichiko Aoba", and
フィッシュマンズ "Fishmans", by MusicBrainz's own primary alias for the language. Nothing is
transliterated or guessed: without such an alias the original name stays."""
import unicodedata
from typing import Optional

from .. import config
from ..upstream import musicbrainz


def is_latin(text: Optional[str]) -> bool:
    """Every letter is a Latin one (digits, punctuation and spaces don't count)."""
    letters = [ch for ch in text or "" if ch.isalpha()]
    return all(unicodedata.name(ch, "").startswith("LATIN") for ch in letters)


def pick(mb: dict, locale: str) -> Optional[dict]:
    """{name, sort, original} from MusicBrainz's names, or None when the artist keeps its own."""
    name = mb.get("name")
    if not name or not locale or locale == "original" or is_latin(name):
        return None
    lang = locale.split("_")[0]
    for alias in mb.get("aliases") or []:
        if alias["primary"] and (alias.get("locale") or "").split("_")[0] == lang and alias["name"] != name:
            sort = mb.get("sortName") if is_latin(mb.get("sortName")) else alias["name"]
            return {"name": alias["name"], "sort": sort or alias["name"], "original": name}
    return None


async def preferred(artist_mbid: Optional[str]) -> Optional[dict]:
    """The library name for a MusicBrainz artist under NAME_LOCALE, or None to keep its own."""
    if not artist_mbid or config.settings.name_locale in ("", "original"):
        return None
    mb = await musicbrainz.artist_names(artist_mbid)
    return pick(mb, config.settings.name_locale) if mb else None
