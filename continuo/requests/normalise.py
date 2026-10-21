"""The one `normalise()` used by search merging, matching and dedupe (the fix/88 lesson: curly
quotes and punctuation must never split one album into two)."""
import re
import unicodedata

_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
})
_AND = re.compile(r"\s*(&|\+|\band\b)\s*")
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)
_LEADING_THE = re.compile(r"^the ")
# Edition noise that should not stop "Geogaddi (Remastered)" matching "Geogaddi".
_EDITION = re.compile(
    r"\s*[\(\[][^)\]]*\b(remaster(ed)?|deluxe|expanded|edition|anniversary|bonus|reissue|"
    r"version|mono|stereo)\b[^)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)


def normalise(s: str | None) -> str:
    """NFKC, curly→straight quotes, '&'/'+'→'and', strip punctuation/whitespace, casefold.
    Letters outside ASCII are kept (so non-Latin titles don't all collapse to '')."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES).casefold()
    s = _AND.sub(" and ", s)
    s = _LEADING_THE.sub("", s.strip())
    # Drop combining marks so "Sigur Rós" == "Sigur Ros".
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("", s)


def strip_edition(title: str | None) -> str:
    """'Geogaddi (2018 Remaster)' -> 'Geogaddi'. Applied repeatedly for stacked suffixes."""
    t = title or ""
    while True:
        new = _EDITION.sub("", t)
        if new == t:
            return t
        t = new


def album_key(artist: str | None, title: str | None) -> str:
    return f"{normalise(artist)}|{normalise(strip_edition(title))}"


def track_key(artist: str | None, title: str | None) -> str:
    return f"{normalise(artist)}|{normalise(title)}"
