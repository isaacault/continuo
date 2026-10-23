"""An album's folder from its songs' paths (#27: a library links whole album folders)."""
from continuo.upstream.navidrome import album_folder


def test_one_folder():
    assert album_folder(["BoC/Geogaddi/01.flac", "BoC/Geogaddi/02.flac"]) == "BoC/Geogaddi"


def test_per_disc_subfolders_are_one_album():
    assert album_folder(["Fishmans/98.12.28/CD1/01.flac", "Fishmans/98.12.28/CD2/01.flac"]) == "Fishmans/98.12.28"
    assert album_folder(["A/Album/01.flac", "A/Album/CD2/01.flac"]) == "A/Album"


def test_never_a_whole_artist_folder():
    assert album_folder(["A/One/01.flac", "A/Two/01.flac"]) == "A/One", "spread over albums: the first's folder"
    assert album_folder(["A/01.flac", "B/01.flac"]) == "A"
    assert album_folder([]) is None
