from continuo.requests.normalise import album_key, normalise, strip_edition


def test_curly_quotes_and_punctuation_fold():
    # The fix/88 regression: a curly apostrophe must not split one album into two.
    assert normalise("Don’t Look Back") == normalise("Don't Look Back") == "dontlookback"


def test_ampersand_diacritics_and_leading_the():
    assert normalise("Simon & Garfunkel") == normalise("Simon and Garfunkel")
    assert normalise("Sigur Rós") == normalise("Sigur Ros")
    assert normalise("The Beatles") == normalise("Beatles")


def test_non_latin_is_kept():
    assert normalise("坂本龍一") == "坂本龍一"
    assert normalise("") == "" and normalise(None) == ""


def test_edition_suffixes_are_stripped_for_keys():
    assert strip_edition("Geogaddi (2018 Remaster)") == "Geogaddi"
    assert strip_edition("Abbey Road (Super Deluxe Edition) [Remastered]") == "Abbey Road"
    assert strip_edition("Live at the BBC (Part 1)") == "Live at the BBC (Part 1)"
    assert album_key("Boards of Canada", "Geogaddi (Deluxe)") == album_key("boards of canada",
                                                                           "Geogaddi")
