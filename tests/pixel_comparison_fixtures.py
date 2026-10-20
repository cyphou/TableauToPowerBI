"""Independent tiny PNG fixtures: stdlib only, no Desktop or image libraries."""

import socket
import struct
import urllib.request
import zlib
from unittest import mock


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))


def png_header(width=2, height=1, *, depth=8, color=6, interlace=0):
    return png_chunk(b"IHDR", struct.pack(
        ">IIBBBBB", width, height, depth, color, 0, 0, interlace))


def tiny_png(rows=((10, 20, 30, 255, 40, 50, 60, 255),), *,
             channels=4, filters=None, level=6):
    """Encode known pixels independently, including PNG filters 0 through 4."""
    width = len(rows[0]) // channels
    previous = [0] * len(rows[0])
    scanlines = bytearray()
    for row, filter_type in zip(rows, filters or [0] * len(rows)):
        scanlines.append(filter_type)
        for index, value in enumerate(row):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            estimate = left + up - upper_left
            distances = [abs(estimate - n) for n in (left, up, upper_left)]
            paeth = (left, up, upper_left)[distances.index(min(distances))]
            predictor = (0, left, up, (left + up) // 2, paeth)[filter_type]
            scanlines.append((value - predictor) & 255)
        previous = row
    return (PNG_SIGNATURE
            + png_header(width, len(rows), color=2 if channels == 3 else 6)
            + png_chunk(b"IDAT", zlib.compress(scanlines, level))
            + png_chunk(b"IEND", b""))


def forbid_network(testcase):
    """Fail immediately if a regression accidentally calls an external service."""
    for owner, name in ((socket.socket, "connect"), (socket, "create_connection"),
                        (urllib.request, "urlopen")):
        testcase.enterContext(mock.patch.object(
            owner, name, side_effect=AssertionError("Pixel tests must remain offline")))
