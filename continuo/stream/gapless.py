"""Gapless Opus over HLS: what each rendition's init segment must say, and the sums behind it.

ffmpeg's fMP4 HLS output keeps Opus pre-skip (in `dOps`) and shortens the last sample's duration
to the song's exact end, but a decoder always outputs whole 20 ms packets, and Media3 trims a
song's end only from Format.encoderPadding, which it reads from an iTunes `iTunSMPB` tag in
moov/udta/meta/ilst. ffmpeg writes no such tag.

Media3 1.11.1 also misreads `dOps`: it copies the big-endian box into a little-endian OpusHead, so
PreSkip 312 (0x0138) becomes 14337 and ~0.3 s is cut from every song (androidx/media#3431, fixed
after 1.11.1). A PreSkip of 0 reads the same either way, so continuo zeroes it and states the
real delay in iTunSMPB with the padding; Media3's TrimmingAudioProcessor then trims both ends.

Proved in Polyphony's HlsGaplessSpikeTest (docs/spikes/hls-gapless in the Polyphony repo): three
tracks cut from one tone join to within 2 samples at 48 kHz.
"""
import math
import struct
from dataclasses import dataclass

OPUS_RATE = 48_000
OPUS_FRAME = 960            # libopus' default 20 ms packets
SEGMENT_SECONDS = 6         # a whole number of packets (300)


@dataclass(frozen=True)
class Plan:
    """What an encode of `samples` (at 48 kHz, after resampling) will produce."""
    samples: int            # the song's own length at 48 kHz
    preskip: int            # the encoder's delay, in the declared timeline before the song
    packets: int
    padding: int            # decoded samples after the song's end, to trim
    segments: tuple[float, ...]   # each segment's declared duration in seconds


def resampled_count(samples: int, rate: int) -> int:
    """How many 48 kHz samples ffmpeg's resampler makes of `samples` at `rate` (it rounds up)."""
    if rate == OPUS_RATE:
        return samples
    return math.ceil(samples * OPUS_RATE / rate)


def plan(samples: int, preskip: int = 312) -> Plan:
    """The encode's packets, tail padding and segment durations, before running it.

    The declared timeline starts at 0 with the pre-skip: ffmpeg's segments are 6 s of it each, and
    the last is what remains."""
    declared = preskip + samples
    packets = math.ceil(declared / OPUS_FRAME)
    per_segment = SEGMENT_SECONDS * OPUS_RATE
    full, rest = divmod(declared, per_segment)
    durations = [float(SEGMENT_SECONDS)] * full + ([rest / OPUS_RATE] if rest else [])
    return Plan(samples, preskip, packets, packets * OPUS_FRAME - declared, tuple(durations))


# --- MP4 boxes -----------------------------------------------------------------------------------

def _children(data: bytes | bytearray, start: int, end: int):
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        if size < 8 or i + size > end:
            raise ValueError(f"bad box {kind!r} at {i}")
        yield kind, i, size
        i += size


def _find(data, start: int, end: int, kind: bytes):
    return next(((o, s) for k, o, s in _children(data, start, end) if k == kind), None)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _full_box(kind: bytes, payload: bytes) -> bytes:
    return _box(kind, b"\0\0\0\0" + payload)


def _dops(data) -> int:
    """Offset of the dOps box (moov/trak/mdia/minf/stbl/stsd/Opus/dOps)."""
    moov = _find(data, 0, len(data), b"moov")
    if moov is None:
        raise ValueError("no moov")
    at, end = moov[0] + 8, moov[0] + moov[1]
    for kind in (b"trak", b"mdia", b"minf", b"stbl", b"stsd"):
        box = _find(data, at, end, kind)
        if box is None:
            raise ValueError(f"no {kind.decode()}")
        at, end = box[0] + 8, box[0] + box[1]
    # stsd: full box + entry count, then the Opus sample entry (8 + 28 bytes before its children).
    entry = _find(data, at + 8, end, b"Opus")
    if entry is None:
        raise ValueError("not Opus")
    dops = _find(data, entry[0] + 36, entry[0] + entry[1], b"dOps")
    if dops is None:
        raise ValueError("no dOps")
    return dops[0]


def preskip(init: bytes) -> int:
    """dOps PreSkip, read big-endian as ISO BMFF Opus defines it."""
    at = _dops(init)
    return struct.unpack(">H", init[at + 10:at + 12])[0]


def itunsmpb(delay: int, padding: int, samples: int) -> str:
    return " 00000000 %08X %08X %016X" % (delay, padding, samples) + " 00000000" * 8


def rewrite_init(init: bytes, delay: int, padding: int, samples: int) -> bytes:
    """The init segment with dOps PreSkip zeroed and iTunSMPB (delay, padding) added to
    moov/udta/meta/ilst, merged into what ffmpeg wrote there (it adds ©too) so there is one meta."""
    data = bytearray(init)
    at = _dops(data)
    data[at + 10:at + 12] = b"\0\0"
    freeform = _box(b"----", _full_box(b"mean", b"com.apple.iTunes")
                    + _full_box(b"name", b"iTunSMPB")
                    + _box(b"data", struct.pack(">II", 1, 0)
                           + itunsmpb(delay, padding, samples).encode()))
    moov = _find(data, 0, len(data), b"moov")
    ancestors = [moov[0]]
    udta = _find(data, moov[0] + 8, moov[0] + moov[1], b"udta")
    hdlr = _full_box(b"hdlr", struct.pack(">I4s", 0, b"mdir") + b"appl" + b"\0" * 9)
    if udta is None:
        insert_at = moov[0] + moov[1]
        payload = _box(b"udta", _full_box(b"meta", hdlr + _box(b"ilst", freeform)))
    else:
        ancestors.append(udta[0])
        meta = _find(data, udta[0] + 8, udta[0] + udta[1], b"meta")
        if meta is None:
            insert_at = udta[0] + udta[1]
            payload = _full_box(b"meta", hdlr + _box(b"ilst", freeform))
        else:
            ancestors.append(meta[0])
            ilst = _find(data, meta[0] + 12, meta[0] + meta[1], b"ilst")
            if ilst is None:
                insert_at, payload = meta[0] + meta[1], _box(b"ilst", freeform)
            else:
                ancestors.append(ilst[0])
                insert_at, payload = ilst[0] + ilst[1], freeform
    data[insert_at:insert_at] = payload
    for off in ancestors:
        size = struct.unpack(">I", data[off:off + 4])[0]
        data[off:off + 4] = struct.pack(">I", size + len(payload))
    return bytes(data)


def segment_samples(segment: bytes) -> tuple[int, int]:
    """(packets, declared samples) in one fMP4 segment: its trun entries, durations from trun or
    tfhd's default."""
    packets = declared = 0
    for kind, off, size in _children(segment, 0, len(segment)):
        if kind != b"moof":
            continue
        for k2, o2, s2 in _children(segment, off + 8, off + size):
            if k2 != b"traf":
                continue
            default = 0
            for k3, o3, _s3 in _children(segment, o2 + 8, o2 + s2):
                body = o3 + 8
                flags = int.from_bytes(segment[body + 1:body + 4], "big")
                if k3 == b"tfhd":
                    p = body + 8
                    p += 8 if flags & 0x1 else 0
                    p += 4 if flags & 0x2 else 0
                    if flags & 0x8:
                        default = struct.unpack(">I", segment[p:p + 4])[0]
                elif k3 == b"trun":
                    n = struct.unpack(">I", segment[body + 4:body + 8])[0]
                    p = body + 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
                    for _ in range(n):
                        d = default
                        if flags & 0x100:
                            d = struct.unpack(">I", segment[p:p + 4])[0]
                            p += 4
                        p += 4 * bool(flags & 0x200) + 4 * bool(flags & 0x400) + 4 * bool(flags & 0x800)
                        declared += d
                        packets += 1
    return packets, declared
