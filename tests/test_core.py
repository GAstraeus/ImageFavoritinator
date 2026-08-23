"""Unit tests for the non-GUI logic in photo_viewer.py."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photo_viewer import (
    FavoritesStore,
    apply_filter,
    natural_key,
    pick_view_index,
    rel_name,
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


if __name__ == "__main__":
    unittest.main()
