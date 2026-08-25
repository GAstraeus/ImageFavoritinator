"""GUI smoke test for the native (AppKit + ImageIO) backend.

Run manually on a Mac with a window server (it opens a window briefly):

    python3 tests/smoke_native.py

Skips itself with exit code 0 when PyObjC isn't installed, so it is safe to
run anywhere.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

try:
    import Quartz
    from AppKit import NSApplication, NSEvent, NSScreen
    from Foundation import NSDate, NSRunLoop
except ImportError as exc:
    print(f"skipped: PyObjC not installed ({exc})")
    sys.exit(0)

import native_viewer
from native_viewer import NativeViewer, decode_native
from photo_viewer import BROWSE_TIER, FULL_TIER, Decoded, FavoritesStore, ImageLoader, scan_images

BROWSE_SIDE = 1000
FULL_SIZE = (4000, 3000)


def make_big_image(path, size=FULL_SIZE):
    img = Image.new("RGB", size, "#202020")
    for x in range(0, size[0], 8):
        for y in range(0, size[1], 8):
            img.putpixel((x, y), (255, 255, 255))
    img.save(path, quality=92)


def pump(seconds=0.05):
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds))


def wait_until(predicate, timeout=20.0):
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        pump(0.05)
        waited += 0.05
    return False


def key_event(chars, code=0):
    """Synthesise a key-down event the way AppKit would deliver one."""
    return NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        10, (0, 0), 0, 0, 0, None, chars, chars, False, code)


def pixels_of(image):
    return (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image))


def test_native_viewer():
    NSApplication.sharedApplication()

    tmp = tempfile.TemporaryDirectory()
    directory = Path(tmp.name)
    make_big_image(directory / "BIG_1.jpg")
    make_big_image(directory / "BIG_2.jpg")

    files = scan_images(directory)
    store = FavoritesStore(directory)
    loader = ImageLoader(browse_side=BROWSE_SIDE, decoder=decode_native)
    viewer = NativeViewer(directory, files, store, loader)
    try:
        scale = viewer.backing_scale
        assert scale >= 1.0, scale

        assert wait_until(lambda: isinstance(
            loader.get_cached(viewer.current_file(), BROWSE_TIER), Decoded)), \
            "browse tier never loaded"
        viewer.render()

        # The browse tier holds a small preview but reports the true size, and
        # the canvas is sized from the true size so geometry never shifts.
        browse = loader.get_cached(viewer.current_file(), BROWSE_TIER)
        assert browse.orig_size == FULL_SIZE, browse.orig_size
        assert max(pixels_of(browse.image)) == BROWSE_SIDE, pixels_of(browse.image)

        frame = viewer.canvas.frame().size
        expected = native_viewer.points_for(FULL_SIZE, scale)
        assert abs(frame.width - expected[0]) < 0.5, (frame.width, expected)
        assert abs(frame.height - expected[1]) < 0.5, (frame.height, expected)

        # Fit view: whole image visible, and not upscaled past 100%.
        assert viewer.magnification is None
        fit = viewer.fit_magnification()
        assert 0 < fit <= 1.0, fit
        assert abs(viewer.scroll.magnification() - fit) < 1e-6, \
            (viewer.scroll.magnification(), fit)
        assert viewer._detail_note.startswith("preview"), viewer._detail_note

        # THE POINT OF THIS BACKEND: at 100% one image pixel lands on exactly
        # one device pixel, which tkinter cannot do on a Retina display.
        viewer.zoom_to_actual()
        assert abs(viewer.scroll.magnification() - 1.0) < 1e-6, \
            viewer.scroll.magnification()
        device_px = (frame.width * viewer.scroll.magnification() * scale,
                     frame.height * viewer.scroll.magnification() * scale)
        assert abs(device_px[0] - FULL_SIZE[0]) < 1.0, device_px
        assert abs(device_px[1] - FULL_SIZE[1]) < 1.0, device_px

        # Full-resolution tier arrives; the frame must not move, so swapping
        # the preview for real pixels is invisible except for the detail.
        viewer.load_full()
        assert wait_until(lambda: isinstance(
            loader.get_cached(viewer.current_file(), FULL_TIER), Decoded)), \
            "full tier never loaded"
        viewer.render()
        full = loader.get_cached(viewer.current_file(), FULL_TIER)
        assert pixels_of(full.image) == FULL_SIZE, pixels_of(full.image)
        after = viewer.canvas.frame().size
        assert (after.width, after.height) == (frame.width, frame.height), \
            (after, frame)
        assert viewer._detail_note == "full res", viewer._detail_note

        # A pan is picked up from the scroll view itself (bounds-changed
        # notification), not just from our own zoom calls — otherwise a
        # trackpad pan would be forgotten and the next render would snap the
        # image back.
        clip = viewer.scroll.contentView()
        visible = clip.bounds().size
        slack = (frame.width - visible.width, frame.height - visible.height)
        assert min(slack) > 10, \
            f"window too big for this image to scroll; slack {slack}"
        clip.scrollToPoint_((slack[0] * 0.6, slack[1] * 0.6))
        viewer.scroll.reflectScrolledClipView_(clip)
        assert viewer.center != (0.5, 0.5), \
            "pan was not recorded — bounds observer not wired up"

        # Re-rendering (which is what happens when the full-resolution copy
        # lands) must leave the viewport exactly where the user left it.
        panned = clip.bounds()
        panned_center = viewer.center
        viewer.render()
        after_render = clip.bounds()
        assert abs(after_render.origin.x - panned.origin.x) < 0.5 and \
            abs(after_render.origin.y - panned.origin.y) < 0.5, \
            (after_render.origin, panned.origin)
        assert viewer.center == panned_center, (viewer.center, panned_center)

        # Rendering an image whose pixels haven't arrived yet shrinks the canvas
        # to the viewport, which scrolls it as a side effect. That must not be
        # mistaken for the user panning, or arrowing quickly through a burst
        # would silently throw away the zoom position you parked on.
        real_source_for = viewer._source_for
        viewer._source_for = lambda path: (None, None)
        try:
            viewer.render()
            assert viewer.center == panned_center, \
                f"loading placeholder reset the pan: {viewer.center}"
            viewer.view = []                     # the empty-view branch too
            viewer.render()
            assert viewer.center == panned_center, \
                f"empty view reset the pan: {viewer.center}"
        finally:
            viewer.view = list(files)
            viewer._source_for = real_source_for
        viewer.render()

        # Panning past the edge is pulled back inside the image, never showing
        # empty space beyond it.
        clip.scrollToPoint_((frame.width * 2, frame.height * 2))
        viewer.scroll.reflectScrolledClipView_(clip)
        viewer.render()
        edge = clip.bounds()
        assert -0.5 <= edge.origin.x <= frame.width - edge.size.width + 0.5, \
            (edge.origin.x, frame.width, edge.size.width)
        assert -0.5 <= edge.origin.y <= frame.height - edge.size.height + 0.5, \
            (edge.origin.y, frame.height, edge.size.height)
        assert 0.0 <= viewer.center[0] <= 1.0 and 0.0 <= viewer.center[1] <= 1.0, \
            viewer.center

        # A background load arriving mid-pinch must not yank the zoom away.
        viewer.canvas.viewWillStartLiveMagnify()
        viewer.scroll.setMagnification_(3.0)
        viewer.render()
        assert abs(viewer.scroll.magnification() - 3.0) < 1e-6, \
            viewer.scroll.magnification()
        viewer.canvas.viewDidEndLiveMagnify()
        assert abs(viewer.magnification - 3.0) < 1e-6, viewer.magnification
        viewer.zoom_to_actual()

        # Zoom and position survive navigation (checking focus across a burst).
        zoom_before, center_before = viewer.magnification, viewer.center
        viewer.canvas.keyDown_(key_event(""))        # right arrow
        assert viewer.current_file().name == "BIG_2.jpg", viewer.current_file()
        assert viewer.magnification == zoom_before, (viewer.magnification,
                                                     zoom_before)
        assert viewer.center == center_before, (viewer.center, center_before)

        # Zooming out past fit snaps back to fit.
        viewer.zoom_by(0.01)
        assert viewer.magnification is None, viewer.magnification

        # Magnifying stays smooth like Preview unless you ask for hard pixels.
        assert native_viewer.interpolation_for(3.0) == Quartz.kCGInterpolationHigh
        assert native_viewer.interpolation_for(0.4) == Quartz.kCGInterpolationHigh
        assert not viewer.pixel_peep
        viewer.canvas.keyDown_(key_event("p"))
        assert viewer.pixel_peep
        assert native_viewer.interpolation_for(
            3.0, viewer.pixel_peep) == Quartz.kCGInterpolationNone
        viewer.canvas.keyDown_(key_event("p"))
        assert not viewer.pixel_peep

        # Keys: favorite, filter, and the star overlay following along.
        viewer.canvas.keyDown_(key_event(" "))
        assert store.favorites == {"BIG_2.jpg"}, store.favorites
        assert not viewer.star.isHidden()
        viewer.canvas.keyDown_(key_event("2"))             # favorites only
        assert [p.name for p in viewer.view] == ["BIG_2.jpg"], viewer.view
        viewer.canvas.keyDown_(key_event(" "))             # unfavorite
        assert store.favorites == set()
        assert viewer.star.isHidden()
        viewer.canvas.keyDown_(key_event("2"))             # re-press refreshes
        assert viewer.view == []
        viewer.render()                                    # empty view: no crash
        viewer.canvas.keyDown_(key_event(""))        # navigate empty view
        viewer.canvas.keyDown_(key_event("1"))
        assert len(viewer.view) == 2

        # Help overlay and export.
        viewer.canvas.keyDown_(key_event("h"))
        assert viewer.show_help and not viewer.help.isHidden()
        viewer.canvas.keyDown_(key_event("h"))
        assert viewer.help.isHidden()
        viewer.export_lists()
        assert (directory / "non_favorites.txt").read_text().split() == \
            ["BIG_1.jpg", "BIG_2.jpg"]

        # An unreadable file reports the error instead of dying.
        bad = directory / "BROKEN.jpg"
        bad.write_bytes(b"not an image")
        try:
            decode_native(bad)
            raise AssertionError("expected a decode failure")
        except RuntimeError:
            pass

        pump(0.05)
        print("native viewer OK "
              f"(backingScaleFactor {scale:g}, fit {fit * 100:.0f}%)")
    finally:
        # Not viewer.quit(): that terminates the process, which would take the
        # test runner with it.
        if viewer._timer is not None:
            viewer._timer.invalidate()
        loader.shutdown()
        viewer.window.setDelegate_(None)
        viewer.window.close()
        tmp.cleanup()


if __name__ == "__main__":
    test_native_viewer()
