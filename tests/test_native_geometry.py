"""Unit tests for the native backend's zoom/pan math (no window needed).

Skipped automatically when PyObjC isn't installed.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import native_viewer
except SystemExit as exc:                # native_viewer exits without PyObjC
    native_viewer = None
    SKIP_REASON = str(exc)
except ImportError as exc:
    native_viewer = None
    SKIP_REASON = str(exc)
else:
    SKIP_REASON = ""


@unittest.skipIf(native_viewer is None, f"PyObjC not available: {SKIP_REASON}")
class TestNativeGeometry(unittest.TestCase):
    def test_points_put_one_pixel_on_one_device_pixel(self):
        # The whole point of this backend: on a 2x display a 4000 px image
        # occupies 2000 points, so 100% magnification is pixel exact.
        self.assertEqual(native_viewer.points_for((4000, 3000), 2.0),
                         (2000.0, 1500.0))
        self.assertEqual(native_viewer.points_for((4000, 3000), 1.0),
                         (4000.0, 3000.0))

    def test_points_survive_a_missing_scale_factor(self):
        self.assertEqual(native_viewer.points_for((800, 600), 0),
                         (800.0, 600.0))

    def test_fit_uses_the_tighter_axis(self):
        self.assertAlmostEqual(
            native_viewer.fit_magnification((2000, 1500), (1000, 900)), 0.5)
        self.assertAlmostEqual(
            native_viewer.fit_magnification((2000, 1500), (1800, 450)), 0.3)

    def test_fit_never_upscales_past_actual_pixels(self):
        self.assertEqual(
            native_viewer.fit_magnification((100, 80), (1000, 900)), 1.0)

    def test_fit_survives_a_zero_sized_window(self):
        self.assertEqual(native_viewer.fit_magnification((0, 0), (10, 10)), 1.0)
        self.assertEqual(native_viewer.fit_magnification((10, 10), (0, 0)), 1.0)

    def test_origin_centers_and_clamps(self):
        self.assertEqual(
            native_viewer.origin_for_center((0.5, 0.5), (1000, 800), (400, 300)),
            (300.0, 250.0))
        # Asking for a centre beyond the edge lands flush against the edge,
        # never showing blank space past the image.
        self.assertEqual(
            native_viewer.origin_for_center((1.0, 1.0), (1000, 800), (400, 300)),
            (600, 500))
        self.assertEqual(
            native_viewer.origin_for_center((0.0, 0.0), (1000, 800), (400, 300)),
            (0, 0))

    def test_origin_centers_an_image_smaller_than_the_viewport(self):
        # Negative origin is how a clip view centres a small document view.
        self.assertEqual(
            native_viewer.origin_for_center((0.9, 0.9), (1000, 800), (1200, 900)),
            (-100.0, -50.0))

    def test_center_round_trips_through_origin(self):
        doc, visible = (4000.0, 3000.0), (900.0, 700.0)
        for center in ((0.5, 0.5), (0.25, 0.75), (0.2, 0.2)):
            origin = native_viewer.origin_for_center(center, doc, visible)
            back = native_viewer.center_for_rect((origin, visible), doc)
            self.assertAlmostEqual(back[0], center[0], places=6)
            self.assertAlmostEqual(back[1], center[1], places=6)

    def test_center_for_rect_is_normalized_and_clamped(self):
        self.assertEqual(
            native_viewer.center_for_rect(((0, 0), (100, 100)), (1000, 1000)),
            (0.05, 0.05))
        self.assertEqual(
            native_viewer.center_for_rect(((5000, 5000), (100, 100)),
                                          (1000, 1000)),
            (1.0, 1.0))
        self.assertEqual(
            native_viewer.center_for_rect(((0, 0), (10, 10)), (0, 0)),
            (0.5, 0.5))

    def test_zoom_is_smooth_by_default_like_preview(self):
        """The regression this guards: nearest-neighbour reads as pixelated.

        Blocky nearest-neighbour past 2x was the old default, and it looked
        pixelated even though no detail was missing. Preview.app smooths at
        every zoom level, so we do too.
        """
        import Quartz
        for magnification in (0.4, 1.0, 1.5, 2.0, 4.0, 8.0):
            self.assertEqual(native_viewer.interpolation_for(magnification),
                             Quartz.kCGInterpolationHigh, magnification)

    def test_pixel_peeping_shows_hard_pixels_only_when_magnifying(self):
        import Quartz
        self.assertEqual(
            native_viewer.interpolation_for(3.0, pixel_peep=True),
            Quartz.kCGInterpolationNone)
        # Downscaling with nearest would alias badly, so it stays smooth.
        self.assertEqual(
            native_viewer.interpolation_for(0.5, pixel_peep=True),
            Quartz.kCGInterpolationHigh)
        self.assertEqual(
            native_viewer.interpolation_for(1.0, pixel_peep=True),
            Quartz.kCGInterpolationHigh)

    def test_symbols_are_bound_eagerly_for_thread_safety(self):
        # PyObjC's lazy symbol lookup is not thread-safe (it ends in a dict
        # pop), so worker-thread decoding must only touch resolved names.
        for name in ("CGImageSourceCreateWithURL",
                     "CGImageSourceCopyPropertiesAtIndex",
                     "CGImageSourceCreateThumbnailAtIndex",
                     "CGImageGetWidth", "CGImageGetHeight",
                     "kCGImageSourceThumbnailMaxPixelSize",
                     "kCGImagePropertyOrientation"):
            self.assertTrue(hasattr(native_viewer, name), name)

    def test_decoder_is_thread_safe_under_contention(self):
        """The regression this guards: KeyError from concurrent decodes."""
        import tempfile
        import threading

        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(8):
                path = Path(tmp) / f"IMG_{i}.jpg"
                Image.new("RGB", (900, 600), (10 * i, 90, 120)).save(path)
                paths.append(path)

            failures = []

            def decode(path):
                try:
                    native_viewer.decode_native(path, 300)
                except Exception as exc:          # noqa: BLE001 - reporting
                    failures.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=decode, args=(p,))
                       for p in paths]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
