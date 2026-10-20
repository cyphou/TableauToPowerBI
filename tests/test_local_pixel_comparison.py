"""Permanent strict PNG regressions with exact, independently known metrics."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zlib

from powerbi_import import local_pixel_comparison as pixels
from tests.pixel_comparison_fixtures import (
    PNG_SIGNATURE, forbid_network, png_chunk, png_header, tiny_png,
)


class TestLocalPixelComparison(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        forbid_network(self)

    def compare(self, source, target=None):
        target = source if target is None else target
        a, b = self.root / "source.png", self.root / "target.png"
        a.write_bytes(source)
        b.write_bytes(target)
        result = pixels.compare_png_files(a, b)
        self.assertEqual(a.read_bytes(), source)
        self.assertEqual(b.read_bytes(), target)
        return result

    def assert_unavailable(self, result, code, status="not_comparable"):
        self.assertEqual(result["status"], status)
        self.assertEqual(result["reason_code"], code)
        self.assertIsNone(result["metrics"])

    def test_equal_tiny_rgba_has_exact_zero_metrics_and_hashes(self):
        image = tiny_png()
        result = self.compare(image)
        self.assertEqual(result["status"], "compared")
        self.assertIsNone(result["reason_code"])
        self.assertEqual(result["method"], "decoded_png_rgba_absolute_difference_v1")
        self.assertEqual(result["source_dimensions"], {"width": 2, "height": 1})
        self.assertEqual(result["target_dimensions"], result["source_dimensions"])
        self.assertEqual(result["source_sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual(result["target_sha256"], result["source_sha256"])
        self.assertEqual(result["metrics"], {
            "pixel_count": 2, "different_pixel_count": 0,
            "different_pixel_fraction": 0.0, "absolute_channel_difference_sum": 0,
            "mean_absolute_channel_error": 0.0,
            "normalized_mean_absolute_channel_error": 0.0,
        })

    def test_difference_uses_all_four_channels_and_pixel_denominator(self):
        result = self.compare(tiny_png(), tiny_png(
            ((20, 40, 60, 215, 40, 50, 60, 255),)))
        self.assertEqual(result["status"], "compared")
        self.assertEqual(result["metrics"], {
            "pixel_count": 2, "different_pixel_count": 1,
            "different_pixel_fraction": 0.5,
            "absolute_channel_difference_sum": 100,
            "mean_absolute_channel_error": 12.5,
            "normalized_mean_absolute_channel_error": 100 / (8 * 255),
        })

    def test_alpha_only_difference_is_not_dropped_or_composited(self):
        result = self.compare(tiny_png(), tiny_png(
            ((10, 20, 30, 0, 40, 50, 60, 255),)))
        self.assertEqual(result["metrics"]["different_pixel_count"], 1)
        self.assertEqual(result["metrics"]["absolute_channel_difference_sum"], 255)
        self.assertEqual(result["metrics"]["normalized_mean_absolute_channel_error"], 0.125)
        # Even fully transparent RGB is compared literally, not visually composited.
        result = self.compare(tiny_png(((0, 0, 0, 0),)),
                              tiny_png(((255, 0, 0, 0),)))
        self.assertEqual(result["metrics"]["absolute_channel_difference_sum"], 255)

    def test_rgb_is_promoted_to_opaque_rgba(self):
        result = self.compare(tiny_png(((10, 20, 30, 40, 50, 60),), channels=3),
                              tiny_png())
        self.assertEqual(result["status"], "compared")
        self.assertEqual(result["metrics"]["different_pixel_count"], 0)

    def test_all_png_filters_decode_pixels_not_container_bytes(self):
        rows = ((250, 10, 80, 255, 4, 90, 210, 0),
                (3, 80, 200, 128, 255, 1, 30, 255))
        for channels in (3, 4):
            selected = rows if channels == 4 else (
                (250, 10, 80, 4, 90, 210), (3, 80, 200, 255, 1, 30))
            plain = tiny_png(selected, channels=channels, level=0)
            for filter_type in range(5):
                with self.subTest(channels=channels, filter=filter_type):
                    filtered = tiny_png(selected, channels=channels,
                                        filters=[filter_type] * 2, level=9)
                    result = self.compare(plain, filtered)
                    self.assertEqual(result["status"], "compared")
                    self.assertEqual(result["metrics"]["different_pixel_count"], 0)
                    self.assertNotEqual(result["source_sha256"], result["target_sha256"])

    def test_crc_mismatch_on_either_side_is_error_without_metrics(self):
        valid = tiny_png()
        damaged = valid[:-1] + bytes([valid[-1] ^ 1])
        for source, target, side in ((damaged, valid, "source"),
                                     (valid, damaged, "target")):
            with self.subTest(side=side):
                result = self.compare(source, target)
                self.assert_unavailable(result, "corrupt_png", "error")
                self.assertIn(side + ":", result["note"])
                self.assertEqual(result[f"{side}_sha256"], hashlib.sha256(damaged).hexdigest())

    def test_truncation_missing_iend_and_trailing_bytes_are_corrupt(self):
        valid = tiny_png()
        for invalid in (PNG_SIGNATURE, valid[:15], valid[:-5], valid[:-12],
                        valid + b"trailing"):
            with self.subTest(length=len(invalid)):
                self.assert_unavailable(self.compare(invalid), "corrupt_png", "error")

    def test_invalid_zlib_termination_and_scanline_lengths_are_corrupt(self):
        raw = b"\0" + bytes((10, 20, 30, 255, 40, 50, 60, 255))
        streams = (b"not zlib", zlib.compress(raw)[:-1],
                   zlib.compress(raw) + b"garbage",
                   zlib.compress(raw) + zlib.compress(raw),
                   zlib.compress(raw[:-1]), zlib.compress(raw + b"\0"),
                   zlib.compress(b"\5" + raw[1:]))
        for stream in streams:
            with self.subTest(stream=stream):
                image = (PNG_SIGNATURE + png_header()
                         + png_chunk(b"IDAT", stream) + png_chunk(b"IEND", b""))
                self.assert_unavailable(self.compare(image), "corrupt_png", "error")

    def test_unsupported_formats_do_not_produce_zero_metrics(self):
        self.assert_unavailable(self.compare(b"not a PNG"), "unsupported_image")
        for header in (png_header(depth=16), png_header(color=0),
                       png_header(color=3), png_header(interlace=1)):
            with self.subTest(header=header):
                self.assert_unavailable(self.compare(
                    PNG_SIGNATURE + header + tiny_png()[33:]), "unsupported_png")
        for kind in (b"tRNS", b"acTL", b"fcTL", b"fdAT", b"ABCD"):
            with self.subTest(kind=kind):
                image = tiny_png()
                self.assert_unavailable(self.compare(
                    image[:33] + png_chunk(kind, b"\0") + image[33:]),
                    "unsupported_png")

    def test_dimension_mismatch_retains_evidence_without_resizing(self):
        result = self.compare(tiny_png(), tiny_png(((10, 20, 30, 255),)))
        self.assert_unavailable(result, "dimension_mismatch")
        self.assertEqual(result["source_dimensions"], {"width": 2, "height": 1})
        self.assertEqual(result["target_dimensions"], {"width": 1, "height": 1})
        self.assertIsNotNone(result["source_sha256"])
        self.assertIsNotNone(result["target_sha256"])

    def test_missing_file_is_unreadable_not_identical(self):
        result = pixels.compare_png_files(self.root / "missing.png", self.root / "other.png")
        self.assert_unavailable(result, "image_unreadable")
        self.assertIsNone(result["source_sha256"])

    def test_file_and_decoded_limits_are_enforced_before_metrics(self):
        with mock.patch.object(pixels, "MAX_PNG_BYTES", 16):
            self.assert_unavailable(self.compare(tiny_png()), "image_size_limit")
        with mock.patch.object(pixels, "MAX_SCANLINE_BYTES", 8):
            self.assert_unavailable(self.compare(tiny_png()), "image_size_limit")

    def test_invalid_container_order_and_dimensions_are_corrupt(self):
        idat = png_chunk(b"IDAT", zlib.compress(b"\0" + bytes(8)))
        end = png_chunk(b"IEND", b"")
        cases = (
            PNG_SIGNATURE + idat + png_header() + end,
            PNG_SIGNATURE + png_header() + png_header() + idat + end,
            PNG_SIGNATURE + png_header() + end,
            PNG_SIGNATURE + png_header(width=0) + idat + end,
            PNG_SIGNATURE + png_header() + idat + png_chunk(b"tEXt", b"x")
            + idat + end,
        )
        for image in cases:
            with self.subTest(image=image):
                self.assert_unavailable(self.compare(image), "corrupt_png", "error")
