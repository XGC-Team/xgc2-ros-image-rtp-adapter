import pytest
from ros_image_rtp_adapter.h264 import AnnexBAccessUnits


def test_aud_framing_preserves_nals_and_split_start_codes():
    units = [b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x67sps\x00\x00\x01\x65idr",
             b"\x00\x00\x01\x09\xf0\x00\x00\x01\x41delta"]
    parser = AnnexBAccessUnits()
    result = []
    for value in b"".join(units):
        result += parser.feed(bytes([value]))
    result += parser.finish()
    assert result == units


def test_missing_boundary_cannot_grow_memory_without_bound():
    parser = AnnexBAccessUnits(maximum_bytes=32)
    with pytest.raises(ValueError):
        parser.feed(b"x" * 33)


def test_random_chunking_matches_whole_stream_framing():
    import random

    rng = random.Random(7)
    for _trial in range(200):
        units = [(b"\x00\x00\x00\x01" if rng.random() < 0.5 else b"\x00\x00\x01")
                 + b"\x09\xf0" + bytes(rng.choice([0, 1, 9, 0x65, rng.randrange(256)])
                                          for _ in range(rng.randrange(0, 200)))
                 for _ in range(rng.randrange(1, 6))]
        stream = b"".join(units)
        whole = AnnexBAccessUnits()
        expected = whole.feed(stream) + whole.finish()
        parser = AnnexBAccessUnits()
        result, offset = [], 0
        while offset < len(stream):
            size = rng.randrange(1, 48)
            result += parser.feed(stream[offset:offset + size])
            offset += size
        assert result + parser.finish() == expected


def test_large_access_unit_is_scanned_in_linear_time(monkeypatch):
    import ros_image_rtp_adapter.h264 as h264

    scanned = []
    pattern = h264._AUD

    class CountingPattern:
        def search(self, buffer, position=0):
            scanned.append(len(buffer) - position)
            return pattern.search(buffer, position)

        def finditer(self, buffer, position=0):
            scanned.append(len(buffer) - position)
            return pattern.finditer(buffer, position)

    monkeypatch.setattr(h264, "_AUD", CountingPattern())
    idr = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x65" + b"\x5a" * (4 * 1024 * 1024)
    stream = idr + b"\x00\x00\x00\x01\x09\xf0"
    parser = h264.AnnexBAccessUnits()
    units = []
    for offset in range(0, len(stream), 65536):
        units += parser.feed(stream[offset:offset + 65536])
    assert units == [idr]
    # The previous whole-buffer rescan read about 64x the unit for 64 KiB reads.
    assert sum(scanned) < 2 * len(stream)
