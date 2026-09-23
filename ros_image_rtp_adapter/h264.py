"""Bounded framing of encoder Annex-B output with explicitly inserted AUDs."""
import re

_AUD = re.compile(b"\x00\x00(?:\x00)?\x01\x09")
# A 4-byte start code plus the AUD NAL header can straddle two reads.
_BOUNDARY_OVERLAP = 4


class AnnexBAccessUnits:
    """Split an AUD-delimited byte stream into complete access units.

    Each byte is scanned a bounded number of times: the search resumes where
    the previous read stopped (minus a start-code overlap) instead of
    rescanning the whole pending access unit, so a multi-megabyte IDR arriving
    in 64 KiB reads costs linear, not quadratic, time.
    """

    def __init__(self, maximum_bytes=8 * 1024 * 1024):
        self._buffer = bytearray()
        self._maximum = maximum_bytes
        self._scan_from = 0
        self._unit_started = False

    def feed(self, data):
        self._buffer.extend(data)
        units = []
        while True:
            match = _AUD.search(self._buffer, self._scan_from)
            if match is None:
                self._scan_from = max(self._scan_from, len(self._buffer) - _BOUNDARY_OVERLAP)
                break
            if not self._unit_started:
                # Bytes before the first delimiter belong to the first unit.
                self._unit_started = True
                self._scan_from = match.end()
                continue
            end = match.start()
            if end > self._maximum:
                raise ValueError("H264 access unit exceeds bounded preview buffer")
            units.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
            self._scan_from = match.end() - end
        if len(self._buffer) > self._maximum:
            raise ValueError("H264 access unit exceeds bounded preview buffer")
        return units

    def finish(self):
        data = bytes(self._buffer)
        self._buffer.clear()
        self._scan_from = 0
        self._unit_started = False
        return [data] if data and _AUD.search(data) else []
