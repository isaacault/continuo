"""Gapless Opus over HLS (polyphony#185): continuo's init rewrite, the encode plan, playlists.

Fixtures (tests/fixtures/hls): track 1 of Polyphony's HlsGaplessSpikeTest, 441037 samples of a
continuous 440/660 Hz tone at 44.1 kHz encoded by ffmpeg 9.0.2 libopus 64 kbit/s into 6 s fMP4
HLS segments (`ffmpeg_init.mp4`, `0.m4s`, `1.m4s`), and the init segment Media3 1.11.1's
FragmentedMp4Extractor was shown to trim exactly (`media3_verified_init.mp4`).
"""
import struct
from pathlib import Path

import pytest

from continuo.stream import gapless, playlists

FIX = Path(__file__).parent / "fixtures" / "hls"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_ffmpeg_states_preskip_big_endian():
    assert gapless.preskip(_read("ffmpeg_init.mp4")) == 312


def test_rewrite_matches_the_init_media3_trims_exactly():
    init = _read("ffmpeg_init.mp4")
    out = gapless.rewrite_init(init, delay=312, padding=607, samples=480041)
    assert out == _read("media3_verified_init.mp4")


def test_rewrite_zeroes_preskip_and_states_delay_and_padding():
    out = gapless.rewrite_init(_read("ffmpeg_init.mp4"), 312, 607, 480041)
    assert gapless.preskip(out) == 0
    assert b" 00000000 00000138 0000025F 0000000000075329" in out
    # one meta box: the tag joins ffmpeg's ilst (which holds ©too)
    assert out.count(b"meta") == 1 and b"\xa9too" in out


def test_rewrite_adds_udta_when_there_is_none():
    init = _read("ffmpeg_init.mp4")
    moov_at = init.index(b"moov") - 4
    moov_size = struct.unpack(">I", init[moov_at:moov_at + 4])[0]
    udta_at = init.index(b"udta") - 4
    udta_size = struct.unpack(">I", init[udta_at:udta_at + 4])[0]
    stripped = bytearray(init[:udta_at] + init[udta_at + udta_size:])
    stripped[moov_at:moov_at + 4] = struct.pack(">I", moov_size - udta_size)
    out = gapless.rewrite_init(bytes(stripped), 312, 607, 480041)
    assert out.count(b"udta") == 1 and out.count(b"meta") == 1 and b"iTunSMPB" in out
    gapless.preskip(out)  # still parses


def test_rewrite_refuses_what_is_not_opus():
    with pytest.raises(ValueError):
        gapless.rewrite_init(b"\0\0\0\x08free", 312, 0, 0)


@pytest.mark.parametrize("source,expected", [(441037, 480041), (441274, 480299), (440689, 479662)])
def test_resampled_count_matches_ffmpeg(source, expected):
    # What ffmpeg 9.0.2's resampler produced for the spike's three tracks (44.1 -> 48 kHz).
    assert gapless.resampled_count(source, 44_100) == expected


def test_plan_matches_what_ffmpeg_wrote():
    plan = gapless.plan(480041)
    packets = declared = 0
    for name in ("0.m4s", "1.m4s"):
        p, d = gapless.segment_samples(_read(name))
        packets += p
        declared += d
    assert (plan.packets, plan.preskip + plan.samples) == (packets, declared) == (501, 480353)
    assert plan.padding == 607
    assert plan.segments == (6.0, pytest.approx(4.007354, abs=1e-6))


@pytest.mark.parametrize("samples,padding", [(480299, 349), (479662, 26)])
def test_plan_padding_for_the_other_tracks(samples, padding):
    assert gapless.plan(samples).padding == padding


def test_plan_exact_segment_multiple_has_no_empty_tail():
    plan = gapless.plan(6 * 48_000 * 3 - 312)
    assert plan.segments == (6.0, 6.0, 6.0)


def test_levels_respect_the_clients_limits():
    assert playlists.levels() == [32, 64, 128, 256]
    assert playlists.levels(max_kbps=128) == [32, 64, 128]
    assert playlists.levels(max_kbps=16) == [32]
    assert playlists.levels(min_kbps=64, max_kbps=64) == [64]


def test_master_lists_each_rendition():
    text = playlists.master([32, 128])
    assert text.startswith("#EXTM3U\n")
    assert 'AVERAGE-BANDWIDTH=33920,CODECS="opus"\n32/index.m3u8' in text
    assert "128/index.m3u8" in text and "256/" not in text


def test_media_playlist_is_complete_vod():
    text = playlists.media((6.0, 4.007354))
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert '#EXT-X-MAP:URI="init.mp4"' in text
    assert "#EXTINF:6.000000,\n0.m4s\n#EXTINF:4.007354,\n1.m4s" in text
    assert "#EXT-X-TARGETDURATION:6" in text
