"""Strict, bounded PNG pixel diagnostics. No AI, network, or image dependencies."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path
from typing import NoReturn

PIXEL_LIMITATION = (
    "Local non-semantic pixel diagnostic, not SSIM or parity certification. "
    "Compares full-window pixels at identical dimensions without resizing, cropping, "
    "alignment, tolerance, or guessed canvas correspondence. Application chrome, "
    "DPI, fonts, animation and selection state can cause differences. Initial "
    "captures establish workbook pairing only, not selected-page correspondence. "
    "RGBA channels are compared literally (RGB gets alpha 255), without compositing "
    "or color-profile/gamma correction. Only non-interlaced 8-bit RGB/RGBA PNG is "
    "supported; transparency chunks and animated PNG are unsupported. "
    "Files and decoded scanlines are each limited to 64 MiB. "
    "No semantic execution, data parity, refresh, rendering fidelity, or deployment "
    "success is inferred. Metrics never change capture status."
)
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_SCANLINE_BYTES = 64 * 1024 * 1024


class _PNGError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def unavailable_pixels(code: str, note: str, *, status: str = "not_comparable") -> dict:
    """No numeric substitute for missing or invalid evidence."""
    return {"method": "decoded_png_rgba_absolute_difference_v1",
            "status": status, "reason_code": code, "note": note,
            "source_dimensions": None, "target_dimensions": None,
            "source_sha256": None, "target_sha256": None, "metrics": None}


def _decode_strict_png(data: bytes) -> tuple[int, int, bytes]:
    """Validate structure/CRC and decode filters 0–4, preserving alpha.

    equivalence_tester._decode_png is intentionally not reused: it ignores CRC,
    drops alpha, accepts incomplete containers and decompresses without a bound.
    """
    def invalid(message: str) -> NoReturn:
        raise _PNGError("corrupt_png", message)

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _PNGError("unsupported_image", "Not a PNG image")
    offset = 8
    header = None
    compressed = bytearray()
    ended = False
    idat_started = idat_closed = False
    palette_seen = False
    while offset < len(data):
        if offset + 12 > len(data):
            invalid("Truncated PNG chunk")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            invalid("Truncated PNG chunk payload")
        payload = data[offset + 8:end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff != crc:
            invalid("PNG chunk CRC mismatch")
        if any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kind) or kind[2] & 32:
            invalid("Invalid PNG chunk type")
        if header is None and kind != b"IHDR":
            invalid("IHDR must be first")
        if kind == b"IHDR":
            if header is not None or length != 13:
                invalid("Duplicate or invalid IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload)
            if not width or not height or width > 0x7fffffff or height > 0x7fffffff:
                invalid("Invalid PNG dimensions")
            if compression != 0 or filtering != 0 or interlace not in (0, 1):
                invalid("Invalid PNG compression/filter/interlace method")
            if depth != 8 or color not in (2, 6) or interlace:
                raise _PNGError("unsupported_png",
                                "Requires non-interlaced 8-bit RGB/RGBA PNG")
            channels = 3 if color == 2 else 4
            expected = height * (width * channels + 1)
            if expected > MAX_SCANLINE_BYTES or width * height * 4 > MAX_SCANLINE_BYTES:
                raise _PNGError("image_size_limit", "Decoded PNG exceeds 64 MiB limit")
            header = (width, height, channels, expected)
        elif kind == b"IDAT":
            if idat_closed:
                invalid("Nonconsecutive IDAT chunks")
            idat_started = True
            compressed.extend(payload)
        elif kind == b"IEND":
            if length or not idat_started or end != len(data):
                invalid("Invalid IEND, missing IDAT, or trailing bytes")
            ended = True
            break
        else:
            if idat_started:
                idat_closed = True
            if kind in (b"tRNS", b"acTL", b"fcTL", b"fdAT"):
                raise _PNGError("unsupported_png", "Transparency chunks/animated PNG unsupported")
            if kind == b"PLTE":
                if palette_seen or idat_started or not length or length % 3 or length > 768:
                    invalid("Invalid PLTE")
                palette_seen = True
            elif not kind[0] & 32:
                raise _PNGError("unsupported_png", "Unknown critical PNG chunk")
        offset = end
    if not ended or header is None:
        invalid("Missing PNG IEND")
    width, height, channels, expected = header
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, expected + 1)
    except zlib.error as exc:
        raise _PNGError("corrupt_png", "Invalid PNG compressed stream") from exc
    if (len(raw) != expected or not inflater.eof or inflater.unused_data
            or inflater.unconsumed_tail):
        invalid("Invalid PNG scanline length or compressed stream termination")
    stride = width * channels
    previous = bytearray(stride)
    pixels = bytearray()
    for start in range(0, len(raw), stride + 1):
        filter_type = raw[start]
        if filter_type > 4:
            invalid("Invalid PNG scanline filter")
        row = bytearray(raw[start + 1:start + stride + 1])
        if filter_type:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                up = previous[index]
                upper_left = previous[index - channels] if index >= channels else 0
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = up
                elif filter_type == 3:
                    predictor = (left + up) // 2
                else:
                    estimate = left + up - upper_left
                    distances = (abs(estimate - left), abs(estimate - up),
                                 abs(estimate - upper_left))
                    predictor = (left, up, upper_left)[distances.index(min(distances))]
                row[index] = (row[index] + predictor) & 255
        if channels == 4:
            pixels.extend(row)
        else:
            for index in range(0, stride, 3):
                pixels.extend(row[index:index + 3])
                pixels.append(255)
        previous = row
    return width, height, bytes(pixels)


def compare_png_files(source: str | Path, target: str | Path) -> dict:
    """Read-only deterministic pixel metric; caller must verify capture provenance."""
    result = unavailable_pixels("not_decoded", "No pixels compared")
    decoded = []
    for side, path in (("source", source), ("target", target)):
        try:
            with Path(path).open("rb") as stream:
                data = stream.read(MAX_PNG_BYTES + 1)
            if len(data) > MAX_PNG_BYTES:
                raise _PNGError("image_size_limit", "PNG exceeds 64 MiB file limit")
            result[f"{side}_sha256"] = hashlib.sha256(data).hexdigest()
            width, height, pixels = _decode_strict_png(data)
        except _PNGError as exc:
            result.update(status="error" if exc.code == "corrupt_png" else "not_comparable",
                          reason_code=exc.code, note=f"{side}: {exc}")
            return result
        except OSError as exc:
            result.update(status="not_comparable", reason_code="image_unreadable",
                          note=f"{side}: {type(exc).__name__}: {exc}")
            return result
        result[f"{side}_dimensions"] = {"width": width, "height": height}
        decoded.append(pixels)
    if result["source_dimensions"] != result["target_dimensions"]:
        result.update(reason_code="dimension_mismatch",
                      note="Full-window dimensions differ; no resizing/cropping performed")
        return result
    a, b = decoded
    absolute_sum = sum(abs(x - y) for x, y in zip(a, b))
    changed = sum(a[i:i + 4] != b[i:i + 4] for i in range(0, len(a), 4))
    count = len(a) // 4
    result.update(
        status="compared", reason_code=None,
        note="Literal RGBA pixels only; no pass/fail threshold or semantic verdict",
        metrics={"pixel_count": count, "different_pixel_count": changed,
                 "different_pixel_fraction": changed / count,
                 "absolute_channel_difference_sum": absolute_sum,
                 "mean_absolute_channel_error": absolute_sum / len(a),
                 "normalized_mean_absolute_channel_error": absolute_sum / (len(a) * 255)})
    return result
