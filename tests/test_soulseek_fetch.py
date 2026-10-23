"""Picking one song's best copy from slskd search responses (issue #24)."""
from continuo.fetch import soulseek

FORMATS = [("flac", 0), ("mp3", 320)]


def _resp(user, files, free=True, queue=0, speed=1_000_000):
    return {"username": user, "hasFreeUploadSlot": free, "queueLength": queue, "uploadSpeed": speed, "files": files}


def _f(name, ext=None, kbps=None, length=None, size=10):
    return {"filename": name, "extension": ext or name.rsplit(".", 1)[-1], "bitRate": kbps, "length": length, "size": size}


RESPONSES = [
    _resp("slow-flac", [_f(r"@@a\Music\Peach Pit\You and Your Friends\03 - Shampoo Bottles.flac", length=212)], free=False, queue=40),
    _resp("fast-mp3", [_f(r"@@b\Peach Pit\You and Your Friends\03 Shampoo Bottles.mp3", kbps=320, length=213)], speed=9_000_000),
    _resp("low-mp3", [_f(r"@@c\Peach Pit\Shampoo Bottles.mp3", kbps=192, length=213)]),
    _resp("live", [_f(r"@@d\Peach Pit\Live\Shampoo Bottles (Live).flac", length=230)]),
    _resp("other-artist", [_f(r"@@e\Someone\Shampoo Bottles.flac", length=212)]),
    _resp("flac-free", [_f(r"@@f\Peach Pit - You and Your Friends\Peach Pit - Shampoo Bottles.flac", length=211)], queue=2),
]


def test_library_policy_takes_the_preferred_format_first():
    c = soulseek.best(RESPONSES, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.LIBRARY, FORMATS)
    assert c.username == "flac-free"  # FLAC, with a free slot, beats the faster MP3 and the queued FLAC


def test_fastest_policy_takes_the_quickest_good_copy():
    c = soulseek.best(RESPONSES, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.FASTEST, FORMATS)
    assert c.username == "fast-mp3"


def test_wrong_artist_low_bitrate_and_other_versions_are_skipped():
    only_bad = [r for r in RESPONSES if r["username"] in ("low-mp3", "other-artist", "live")]
    assert soulseek.best(only_bad, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.LIBRARY, FORMATS) is None
    # A live version is fine when that's what was asked for.
    c = soulseek.best(only_bad, "Peach Pit", "Shampoo Bottles (Live)", None, soulseek.LIBRARY, FORMATS)
    assert c.username == "live"


def test_a_different_length_is_a_different_recording():
    far = [_resp("x", [_f(r"@@x\Peach Pit\Shampoo Bottles.flac", length=400)])]
    assert soulseek.best(far, "Peach Pit", "Shampoo Bottles", 212_000, soulseek.LIBRARY, FORMATS) is None


def test_download_lands_under_the_remote_parent_folder():
    c = soulseek.candidates([RESPONSES[1]])[0]
    assert c.folder == "You and Your Friends" and c.basename == "03 Shampoo Bottles.mp3"


def test_formats_follow_soularr_when_there_is_no_config():
    assert soulseek.formats() == [("flac", 0), ("mp3", 320)]
