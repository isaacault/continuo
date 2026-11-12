"""Adaptive streaming (polyphony#185): each song as HLS, Opus in fMP4 segments at several bitrates,
encoded on this machine, so the player can change quality between 6-second segments as the
connection changes, and keep albums gapless.

* gapless.py — what each rendition's init segment must say for Media3 to join songs exactly.
* playlists.py — the master and media playlists.
* encoder.py — ffmpeg from Navidrome's original file, the disk cache, how many encode at once.

The phone measures its connection and picks the level; continuo encodes what it asks for, and the
songs coming up next in its queue, ahead. Nothing about past connections decides anything; the
phones' connection samples are kept for looking at (connection.py, GET /v1/admin/connection).
"""
