"""The account's single-song albums (filed under _singles), for the app's Albums/Songs/Artists."""
from conftest import bearer, login, make_user

from continuo.db import library as lib_db


def test_single_song_albums_are_listed_full_albums_are_not(client):
    make_user()
    s = login(client)
    lib_db.upsert_many([
        {"navidrome_id": "nd-cy", "name": "To Cy & Lee: Instrumentals, Vol. 1", "artist": "Alabaster dePlume", "song_count": 1,
         "folder": "_singles/Alabaster dePlume/To Cy & Lee_ Instrumentals, Vol. 1", "mbz_release_group_id": "rg-cy"},
        {"navidrome_id": "nd-full", "name": "Being So Normal", "artist": "Peach Pit", "song_count": 11,
         "folder": "Peach Pit/Being So Normal", "mbz_release_group_id": "rg-bsn"},
    ])
    r = client.get("/v1/library/singles", headers=bearer(s))
    assert r.status_code == 200, r.text
    assert r.json() == {"albums": [{"navidromeAlbumId": "nd-cy", "have": 1, "expected": 1}]}
