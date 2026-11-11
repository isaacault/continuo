"""HLS playlists for one song: a master listing the renditions, and each rendition's media playlist.

The media playlist is complete (VOD) from the first request, computed from the song's length
(gapless.plan), so the player sees an ordinary finished stream while the segments are still
being encoded; a segment it asks for before ffmpeg has written it waits a moment (api/stream.py).
"""
import math

# Opus bitrates, kbit/s. 32 still sounds like music; 256 is transparent.
LADDER: tuple[int, ...] = (32, 64, 128, 256)
# fMP4 boxes and segment headers on top of the audio, for BANDWIDTH.
_OVERHEAD = 1.06

MPEGURL = "application/vnd.apple.mpegurl"


def levels(max_kbps: int | None = None, min_kbps: int | None = None) -> list[int]:
    """The ladder within the client's limits (its data-saver cap, say); never empty."""
    out = [k for k in LADDER
           if (max_kbps is None or k <= max_kbps) and (min_kbps is None or k >= min_kbps)]
    return out or [LADDER[0] if max_kbps is not None and max_kbps < LADDER[0] else LADDER[-1]]


def master(kbps_levels: list[int]) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for k in kbps_levels:
        peak = int(k * 1000 * _OVERHEAD * 1.25)        # VBR: a loud passage runs over the average
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={int(k * 1000 * _OVERHEAD)},'
                     f'CODECS="opus"')
        lines.append(f"{k}/index.m3u8")
    return "\n".join(lines) + "\n"


def media(segments: tuple[float, ...] | list[float]) -> str:
    target = max(1, math.ceil(max(segments))) if segments else 1
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS",
             '#EXT-X-MAP:URI="init.mp4"']
    for i, d in enumerate(segments):
        lines.append(f"#EXTINF:{d:.6f},")
        lines.append(f"{i}.m4s")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
