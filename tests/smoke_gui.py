"""GUI smoke test: drives the real tkinter app on generated images.

Run manually (opens a hidden window briefly):

    python3 tests/smoke_gui.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from photo_viewer import (
    BROWSE_TIER,
    FULL_TIER,
    Decoded,
    FavoritesStore,
    ImageLoader,
    ViewerApp,
    scan_images,
)

CANVAS_W, CANVAS_H = 1200, 800


def make_images(directory, count=5):
    colors = ["#cc3333", "#33cc33", "#3333cc", "#cccc33", "#cc33cc"]
    for i in range(count):
        img = Image.new("RGB", (640, 420), colors[i % len(colors)])
        img.save(directory / f"IMG_{i + 1}.jpg", quality=85)


def make_big_image(path, size=(4000, 3000)):
    """A large image with fine detail, so downscaling is visible in tests."""
    img = Image.new("RGB", size, "#202020")
    for x in range(0, size[0], 8):
        for y in range(0, size[1], 8):
            img.putpixel((x, y), (255, 255, 255))
    img.save(path, quality=92)


def wait_until(root, predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def sized_canvas(app, root):
    """Pin the window down so the drawing path runs; return real canvas size.

    The canvas is packed to fill the window, so its size comes from the
    window geometry, not from a requested width.
    """
    root.geometry(f"{CANVAS_W}x{CANVAS_H}+40+40")
    root.update()
    size = (app.canvas.winfo_width(), app.canvas.winfo_height())
    assert min(size) > 2, f"canvas never got a usable size: {size}"
    return size


def test_culling_flow():
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
    sized_canvas(app, root)

    # Current image decodes in the background; wait for it.
    assert wait_until(root, lambda: loader.get_cached(app.current_file())
                      is not None), "first image never loaded"

    # Navigate: 1 -> 2 -> 3, then favorite IMG_3 and IMG_1.
    app.step(1)
    app.step(1)
    assert app.current_file().name == "IMG_3.jpg"
    app.toggle_favorite()
    # The confirmation flash must survive the render() in the same callback.
    assert "Favorited" in app.status.cget("text"), app.status.cget("text")
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
    # Re-pressing the same view key must refresh membership.
    app.set_filter("fav")
    assert app.view == []
    app.render()                   # empty view: message, no crash
    app.step(1)                    # navigation on empty view: no crash

    app.set_filter("all")

    # Save failures must surface in the UI instead of silently losing data.
    os.chmod(directory, 0o555)
    app.toggle_favorite()
    assert app.save_warning is not None
    assert "NOT SAVED" in app.status.cget("text"), app.status.cget("text")
    os.chmod(directory, 0o755)
    app.toggle_favorite()          # save works again -> warning clears
    assert app.save_warning is None

    # Export.
    app.toggle_favorite()  # star current again so export has content
    app.export_lists()
    favs = (directory / "favorites.txt").read_text().split()
    others = (directory / "non_favorites.txt").read_text().split()
    assert len(favs) + len(others) == 5, (favs, others)

    root.update()
    app.quit()
    tmp.cleanup()
    print("culling flow OK")


def test_zoom_and_full_resolution():
    """Zoom must reach real pixels without rendering the whole image."""
    import tkinter as tk

    tmp = tempfile.TemporaryDirectory()
    directory = Path(tmp.name)
    make_big_image(directory / "BIG_1.jpg")
    make_big_image(directory / "BIG_2.jpg")

    root = tk.Tk()
    root.withdraw()
    files = scan_images(directory)
    store = FavoritesStore(directory)
    # Browse tier deliberately far below the original, so the two tiers differ.
    loader = ImageLoader(browse_side=1000)
    app = ViewerApp(root, directory, files, store, loader)
    canvas_w, canvas_h = sized_canvas(app, root)

    assert wait_until(root, lambda: isinstance(
        loader.get_cached(app.current_file(), BROWSE_TIER), Decoded)), \
        "browse tier never loaded"
    app.render()

    # The viewer knows the true size even while showing a capped preview.
    browse = loader.get_cached(app.current_file(), BROWSE_TIER)
    assert browse.orig_size == (4000, 3000), browse.orig_size
    assert max(browse.image.size) == 1000, browse.image.size
    assert app._geom["ow"] == 4000 and app._geom["oh"] == 3000
    assert app.zoom is None and app._geom["zoom"] < 1.0
    assert app._detail_note.startswith("preview"), app._detail_note

    # Zoom to 100%: still only the visible region is rasterised, so the
    # PhotoImage never exceeds the canvas even for a 12 MP source.
    app.zoom_to_actual()
    assert abs(app._geom["zoom"] - 1.0) < 1e-6, app._geom["zoom"]
    assert app._photo.width() <= canvas_w, app._photo.width()
    assert app._photo.height() <= canvas_h, app._photo.height()
    assert "loading full res" in app._detail_note, app._detail_note

    # Full-resolution tier arrives and takes over.
    app._load_full()
    assert wait_until(root, lambda: isinstance(
        loader.get_cached(app.current_file(), FULL_TIER), Decoded)), \
        "full tier never loaded"
    app.render()
    full = loader.get_cached(app.current_file(), FULL_TIER)
    assert full.image.size == (4000, 3000), full.image.size
    assert app._detail_note == "full res", app._detail_note

    # Pan while zoomed, and stay inside the image.
    app.center = (0.5, 0.5)
    app.on_press(type("E", (), {"x": 600, "y": 400})())
    app.on_drag(type("E", (), {"x": 300, "y": 250})())
    app.on_release(type("E", (), {"x": 300, "y": 250})())
    assert app.center[0] > 0.5 and app.center[1] > 0.5, app.center
    assert 0.0 <= app.center[0] <= 1.0 and 0.0 <= app.center[1] <= 1.0

    # Zooming at a pointer keeps that spot roughly under the pointer.
    before = app._geom
    ix = before["left"] + (300 - before["ox"]) / before["zoom"]
    app.zoom_by(2.0, (300, 400))
    after = app._geom
    ix_after = after["left"] + (300 - after["ox"]) / after["zoom"]
    assert abs(ix - ix_after) < 30, (ix, ix_after)
    assert after["zoom"] > before["zoom"]

    # Zoom and position survive navigation (checking focus across a burst).
    zoom_before, center_before = app._geom["zoom"], app.center
    app.step(1)
    assert app.current_file().name == "BIG_2.jpg"
    assert app.zoom == zoom_before and app.center == center_before

    # Zooming out past fit snaps back to fit.
    app.zoom_by(0.01)
    assert app.zoom is None, app.zoom
    app.zoom_to_fit()
    assert app.zoom is None

    root.update()
    app.quit()
    tmp.cleanup()
    print("zoom / full-resolution OK")


TESTS = {"culling": test_culling_flow, "zoom": test_zoom_and_full_resolution}


def main():
    """Run each test in its own process.

    A second Tk root in one process never gets its geometry processed while
    withdrawn on macOS, so the canvas would report 1x1 and the drawing path
    would be skipped. Separate processes keep the windows hidden and the
    measurements real.
    """
    if len(sys.argv) > 1:
        TESTS[sys.argv[1]]()
        return 0
    import subprocess
    for name in TESTS:
        result = subprocess.run([sys.executable, __file__, name])
        if result.returncode != 0:
            print(f"smoke test FAILED: {name}", file=sys.stderr)
            return result.returncode
    print("smoke test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
