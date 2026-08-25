"""Unit tests for the non-GUI logic in photo_viewer.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

import photo_viewer
from photo_viewer import (
    BROWSE_TIER,
    FULL_TIER,
    FavoritesStore,
    apply_filter,
    clamp,
    decode_image,
    natural_key,
    pick_view_index,
    rel_name,
    resample_for,
    scan_images,
)


class TestScanAndSort(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def touch(self, *names):
        for name in names:
            path = self.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake")

    def test_finds_cr3_case_insensitive_and_sorts_naturally(self):
        self.touch("IMG_10.CR3", "IMG_2.cr3", "IMG_1.CR3", "notes.txt",
                   "pic.jpg", ".hidden.cr3")
        found = [p.name for p in scan_images(self.dir)]
        self.assertEqual(found, ["IMG_1.CR3", "IMG_2.cr3", "IMG_10.CR3",
                                 "pic.jpg"])

    def test_recursive_scan(self):
        self.touch("a.cr3", "sub/b.CR3", "sub/deep/c.jpg")
        flat = [p.name for p in scan_images(self.dir)]
        deep = [rel_name(p, self.dir) for p in scan_images(self.dir,
                                                           recursive=True)]
        self.assertEqual(flat, ["a.cr3"])
        self.assertEqual(deep, ["a.cr3", "sub/b.CR3", "sub/deep/c.jpg"])

    def test_natural_key_orders_numbers_numerically(self):
        names = ["IMG_100.CR3", "IMG_9.CR3", "IMG_10.CR3"]
        self.assertEqual(sorted(names, key=natural_key),
                         ["IMG_9.CR3", "IMG_10.CR3", "IMG_100.CR3"])


class TestFavoritesStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_toggle_persists_immediately_and_reloads(self):
        store = FavoritesStore(self.dir)
        self.assertTrue(store.toggle("IMG_1.CR3"))
        self.assertTrue(store.toggle("café photo.cr3"))  # unicode name
        self.assertFalse(store.toggle("IMG_1.CR3"))      # toggle off

        reloaded = FavoritesStore(self.dir)
        self.assertEqual(reloaded.favorites, {"café photo.cr3"})
        data = json.loads((self.dir / "photoviewer_favorites.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["favorites"], ["café photo.cr3"])

    def test_no_stray_temp_files_left_behind(self):
        store = FavoritesStore(self.dir)
        for i in range(5):
            store.toggle(f"IMG_{i}.CR3")
        leftovers = [p.name for p in self.dir.iterdir()
                     if p.name != "photoviewer_favorites.json"]
        self.assertEqual(leftovers, [])

    def test_corrupt_json_exits_instead_of_clobbering(self):
        (self.dir / "photoviewer_favorites.json").write_text("{not json")
        with self.assertRaises(SystemExit):
            FavoritesStore(self.dir)

    def test_wrong_shape_json_exits_without_clobbering(self):
        path = self.dir / "photoviewer_favorites.json"
        for bad in ('{"favorites": "IMG_0001.CR3"}',   # str, not list
                    '["IMG_0001.CR3"]',                # top-level array
                    '{"favorites": [1, 2]}'):          # non-string entries
            path.write_text(bad)
            with self.assertRaises(SystemExit):
                FavoritesStore(self.dir)
            self.assertEqual(path.read_text(), bad)    # file untouched

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_save_failure_raises_but_keeps_memory_state(self):
        store = FavoritesStore(self.dir)
        os.chmod(self.dir, 0o555)
        try:
            with self.assertRaises(OSError):
                store.toggle("IMG_1.CR3")
            # In-memory change survives so a later successful save keeps it.
            self.assertIn("IMG_1.CR3", store.favorites)
        finally:
            os.chmod(self.dir, 0o755)


class TestFiltering(unittest.TestCase):
    def setUp(self):
        self.dir = Path("/photos")
        self.files = [self.dir / f"IMG_{i}.CR3" for i in range(1, 6)]
        self.favs = {"IMG_2.CR3", "IMG_4.CR3"}

    def test_modes(self):
        names = lambda paths: [p.name for p in paths]
        self.assertEqual(
            names(apply_filter(self.files, self.favs, "all", self.dir)),
            ["IMG_1.CR3", "IMG_2.CR3", "IMG_3.CR3", "IMG_4.CR3", "IMG_5.CR3"])
        self.assertEqual(
            names(apply_filter(self.files, self.favs, "fav", self.dir)),
            ["IMG_2.CR3", "IMG_4.CR3"])
        self.assertEqual(
            names(apply_filter(self.files, self.favs, "unfav", self.dir)),
            ["IMG_1.CR3", "IMG_3.CR3", "IMG_5.CR3"])

    def test_pick_view_index_keeps_current_when_visible(self):
        view = apply_filter(self.files, self.favs, "fav", self.dir)
        self.assertEqual(pick_view_index(view, self.files, self.files[3]), 1)

    def test_pick_view_index_lands_on_nearest_earlier(self):
        view = apply_filter(self.files, self.favs, "fav", self.dir)
        # current IMG_3 is not in fav view -> nearest earlier fav is IMG_2
        self.assertEqual(pick_view_index(view, self.files, self.files[2]), 0)
        # current IMG_5 -> nearest earlier fav is IMG_4
        self.assertEqual(pick_view_index(view, self.files, self.files[4]), 1)

    def test_pick_view_index_empty_and_before_first(self):
        self.assertEqual(pick_view_index([], self.files, self.files[0]), 0)
        view = apply_filter(self.files, self.favs, "fav", self.dir)
        # current IMG_1 is before every favorite -> land on first
        self.assertEqual(pick_view_index(view, self.files, self.files[0]), 0)


class TestDecode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.path = self.dir / "big.jpg"
        Image.new("RGB", (3000, 2000), "#336699").save(self.path, quality=90)

    def test_full_tier_keeps_every_pixel(self):
        decoded = decode_image(self.path, 0)
        self.assertEqual(decoded.image.size, (3000, 2000))
        self.assertEqual(decoded.orig_size, (3000, 2000))
        self.assertEqual(decoded.tier, FULL_TIER)

    def test_browse_tier_caps_but_reports_the_original_size(self):
        decoded = decode_image(self.path, 1200)
        self.assertEqual(max(decoded.image.size), 1200)
        self.assertEqual(decoded.orig_size, (3000, 2000))
        self.assertEqual(decoded.tier, BROWSE_TIER)

    def test_cap_above_the_image_does_not_upscale(self):
        decoded = decode_image(self.path, 9000)
        self.assertEqual(decoded.image.size, (3000, 2000))

    def test_raw_develop_is_a_no_op_for_non_raw_files(self):
        """--raw-develop must not disturb JPEGs, only raw sensor files."""
        plain = decode_image(self.path, 0)
        developed = photo_viewer.raw_develop_decoder()(self.path, 0)
        self.assertEqual(developed.image.size, plain.image.size)
        self.assertEqual(developed.orig_size, plain.orig_size)
        self.assertEqual(developed.tier, FULL_TIER)

    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(-1, 0, 10), 0)
        self.assertEqual(clamp(11, 0, 10), 10)


class TestResampling(unittest.TestCase):
    """The tkinter half of the "why does it still look pixelated" fix."""

    def test_magnifying_is_smooth_by_default_like_preview(self):
        for ratio in (1.0, 1.5, 2.0, 4.0, 8.0):
            self.assertEqual(resample_for(ratio),
                             Image.Resampling.BICUBIC, ratio)

    def test_pixel_peeping_shows_hard_pixels_when_magnifying(self):
        for ratio in (1.0, 2.0, 8.0):
            self.assertEqual(resample_for(ratio, pixel_peep=True),
                             Image.Resampling.NEAREST, ratio)

    def test_downscaling_always_uses_lanczos(self):
        # Nearest on a downscale would alias, so P must not reach this case.
        for peep in (False, True):
            for ratio in (0.1, 0.5, 0.99):
                self.assertEqual(resample_for(ratio, pixel_peep=peep),
                                 Image.Resampling.LANCZOS, (ratio, peep))


class TestBackendSelection(unittest.TestCase):
    def test_native_is_the_default_on_macos_when_pyobjc_is_present(self):
        # Forgetting a flag was how the Retina-softness trap got hit, so the
        # sharp backend has to be what you get without asking.
        args = photo_viewer.build_parser().parse_args(["/tmp"])
        self.assertIsNone(args.backend)
        expected = "native" if photo_viewer.native_available() else "tk"
        self.assertEqual(args.backend or expected, expected)

    def test_backends_are_mutually_exclusive(self):
        parser = photo_viewer.build_parser()
        for pair in (["--native", "--tk"], ["--quicklook", "--native"],
                     ["--tk", "--quicklook"]):
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(pair + ["/tmp"])

    def test_each_backend_flag_selects_itself(self):
        parser = photo_viewer.build_parser()
        for flag, expected in (("--native", "native"),
                               ("--quicklook", "quicklook"),
                               ("--tk", "tk")):
            self.assertEqual(
                parser.parse_args([flag, "/tmp"]).backend, expected)

    def test_native_available_is_false_off_darwin(self):
        with unittest.mock.patch.object(photo_viewer.sys, "platform", "linux"):
            self.assertFalse(photo_viewer.native_available())


if __name__ == "__main__":
    unittest.main()
