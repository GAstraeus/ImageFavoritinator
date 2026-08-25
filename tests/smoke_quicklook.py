"""GUI smoke test for the Quick Look backend.

Run manually on a Mac with a window server (it opens a window briefly):

    python3 tests/smoke_quicklook.py

Skips itself with exit code 0 when PyObjC isn't installed, so it is safe to
run anywhere.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

try:
    from AppKit import NSApplication, NSEvent
    from Foundation import NSDate, NSRunLoop
except ImportError as exc:
    print(f"skipped: PyObjC not installed ({exc})")
    sys.exit(0)

import quicklook_viewer
from quicklook_viewer import QuickLookViewer, image_pixel_size
from photo_viewer import FavoritesStore, scan_images

SIZE = (1600, 1200)


def make_image(path, tint):
    Image.new("RGB", SIZE, tint).save(path, quality=90)


def pump(seconds=0.05):
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds))


def key_event(chars, code=0):
    """Synthesise a key-down the way AppKit would deliver one."""
    return NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        10, (0, 0), 0, 0, 0, None, chars, chars, False, code)


def test_quicklook_viewer():
    NSApplication.sharedApplication()

    tmp = tempfile.TemporaryDirectory()
    directory = Path(tmp.name)
    make_image(directory / "QL_1.jpg", "#334455")
    make_image(directory / "QL_2.jpg", "#556677")

    files = scan_images(directory)
    store = FavoritesStore(directory)
    viewer = QuickLookViewer(directory, files, store)
    try:
        # Dimensions come from metadata, with no decode at all.
        assert image_pixel_size(directory / "QL_1.jpg") == SIZE, \
            image_pixel_size(directory / "QL_1.jpg")
        assert image_pixel_size(directory / "missing.jpg") is None

        # Quick Look is pointed at the current file.
        item = viewer.preview.previewItem()
        assert item is not None and item.path().endswith("QL_1.jpg"), item
        pump(0.3)

        # Keys are intercepted by the window before Quick Look can eat the
        # arrows for its own scrolling.
        viewer.window.sendEvent_(key_event(""))     # right arrow
        assert viewer.current_file().name == "QL_2.jpg", viewer.current_file()
        assert viewer.preview.previewItem().path().endswith("QL_2.jpg")
        viewer.window.sendEvent_(key_event(""))     # left arrow
        assert viewer.current_file().name == "QL_1.jpg", viewer.current_file()

        # A key we do not claim is passed through untouched.
        assert viewer.handle_key(key_event("z")) is False

        # Favoriting, and the filter views that make culling work.
        viewer.window.sendEvent_(key_event(" "))
        assert store.favorites == {"QL_1.jpg"}, store.favorites
        viewer.window.sendEvent_(key_event("2"))          # favorites only
        assert [p.name for p in viewer.view] == ["QL_1.jpg"], viewer.view
        viewer.window.sendEvent_(key_event(" "))          # demote
        assert store.favorites == set()
        viewer.window.sendEvent_(key_event("2"))          # re-press refreshes
        assert viewer.view == []
        assert viewer.current_file() is None
        viewer.update_chrome()                            # empty view: no crash
        viewer.window.sendEvent_(key_event(""))     # navigate empty view
        viewer.window.sendEvent_(key_event("1"))
        assert len(viewer.view) == 2

        # Home / End.
        viewer.window.sendEvent_(key_event(""))     # end
        assert viewer.index == 1, viewer.index
        viewer.window.sendEvent_(key_event(""))     # home
        assert viewer.index == 0, viewer.index

        # Help overlay and export.
        viewer.window.sendEvent_(key_event("h"))
        assert viewer.show_help and not viewer.help.isHidden()
        viewer.window.sendEvent_(key_event("h"))
        assert viewer.help.isHidden()
        viewer.window.sendEvent_(key_event("e"))
        assert (directory / "non_favorites.txt").read_text().split() == \
            ["QL_1.jpg", "QL_2.jpg"]

        # Quick Look's own zoom/scroll state is carried across navigation.
        state = viewer.preview.displayState()
        viewer.window.sendEvent_(key_event(""))
        assert viewer._display_state is not None or state is None

        pump(0.1)
        print("quicklook viewer OK")
    finally:
        # Not window.close(): the delegate terminates the app, which would take
        # the test runner with it.
        viewer.window.setDelegate_(None)
        viewer.preview.close()
        viewer.window.close()
        tmp.cleanup()


if __name__ == "__main__":
    test_quicklook_viewer()
