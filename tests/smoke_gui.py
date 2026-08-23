"""GUI smoke test: drives the real tkinter app on generated images.

Run manually (opens a hidden window briefly):

    python3 tests/smoke_gui.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from photo_viewer import FavoritesStore, ImageLoader, ViewerApp, scan_images


def make_images(directory, count=5):
    colors = ["#cc3333", "#33cc33", "#3333cc", "#cccc33", "#cc33cc"]
    for i in range(count):
        img = Image.new("RGB", (640, 420), colors[i % len(colors)])
        img.save(directory / f"IMG_{i + 1}.jpg", quality=85)


def wait_until(root, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main():
    import tkinter as tk

    tmp = tempfile.TemporaryDirectory()
    directory = Path(tmp.name)
    make_images(directory)

    root = tk.Tk()
    root.withdraw()
    files = scan_images(directory)
    assert [p.name for p in files] == [f"IMG_{i}.jpg" for i in range(1, 6)]

    store = FavoritesStore(directory)
    loader = ImageLoader()
    app = ViewerApp(root, directory, files, store, loader)

    # Current image decodes in the background; wait for it.
    assert wait_until(root, lambda: loader.get_cached(app.current_file())
                      is not None), "first image never loaded"

    # Navigate: 1 -> 2 -> 3, then favorite IMG_3 and IMG_1.
    app.step(1)
    app.step(1)
    assert app.current_file().name == "IMG_3.jpg"
    app.toggle_favorite()
    app.jump(0)
    app.toggle_favorite()
    assert store.favorites == {"IMG_1.jpg", "IMG_3.jpg"}

    saved = json.loads((directory / "photoviewer_favorites.json").read_text())
    assert saved["favorites"] == ["IMG_1.jpg", "IMG_3.jpg"], saved

    # Favorites-only view shows exactly the two starred files.
    app.set_filter("fav")
    assert [p.name for p in app.view] == ["IMG_1.jpg", "IMG_3.jpg"]
    assert wait_until(root, lambda: loader.get_cached(app.current_file())
                      is not None), "favorites view image never loaded"

    # Non-favorites view; landing index stays sane.
    app.set_filter("unfav")
    assert [p.name for p in app.view] == ["IMG_2.jpg", "IMG_4.jpg",
                                          "IMG_5.jpg"]

    # Un-favorite everything, then the favorites view must be empty and
    # rendering it must not crash.
    app.set_filter("fav")
    assert app.current_file().name == "IMG_1.jpg"
    app.toggle_favorite()          # IMG_1 off
    app.step(1)
    app.toggle_favorite()          # IMG_3 off
    assert store.favorites == set()
    app.set_filter("all")
    app.set_filter("fav")
    assert app.view == []
    app.render()                   # empty view: message, no crash
    app.step(1)                    # navigation on empty view: no crash

    # Export.
    app.set_filter("all")
    app.toggle_favorite()  # star current again so export has content
    app.export_lists()
    favs = (directory / "favorites.txt").read_text().split()
    others = (directory / "non_favorites.txt").read_text().split()
    assert len(favs) + len(others) == 5, (favs, others)

    root.update()
    app.quit()
    tmp.cleanup()
    print("smoke test OK")


if __name__ == "__main__":
    main()
