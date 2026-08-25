"""Culling viewer that renders with Apple's Quick Look view.

This is the most literal answer to "can we piggyback on Preview?". Preview.app
itself can't be driven -- nothing tells you which image it is showing and there
is no way to hook its keys -- but QLPreviewView is the same renderer behind
Finder's spacebar preview, it is an ordinary NSView, and it takes a file URL.
So the image area is entirely Apple's code while the culling shell (keys,
favorites, filters, status) stays ours.

Trade-offs against --native, which is why that one is still the default:

  * Quick Look does its own loading, so there is no two-tier prefetch. Holding
    the arrow key down is less instant than --native, which keeps screen-sized
    previews of the neighbours in memory.
  * Zoom and scroll belong to Quick Look. We save and restore its displayState
    across navigation, which usually preserves them, but there is no API to set
    a precise magnification the way NSScrollView allows.
  * QLPreviewView reports no pixel dimensions, so the size in the status bar
    comes from reading the file's metadata separately.

Run it with:  python3 photo_viewer.py /photos --quicklook
"""

import os
import subprocess
import sys
from pathlib import Path

try:
    import objc
    from Foundation import NSObject
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSColor,
        NSFont,
        NSMakeRect,
        NSScreen,
        NSTextField,
        NSView,
        NSViewHeightSizable,
        NSViewMaxYMargin,
        NSViewMinYMargin,
        NSViewWidthSizable,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSURL
except ImportError as exc:                                  # pragma: no cover
    sys.exit(
        f"The --quicklook backend needs PyObjC ({exc}). Install it with:\n"
        "    python3 -m pip install pyobjc-framework-Quartz\n"
        "Or drop --quicklook to use the built-in viewer."
    )

from photo_viewer import (
    FILTER_LABELS,
    apply_filter,
    pick_view_index,
    rel_name,
)

# PyObjC doesn't surface QuickLookUI, so load the framework by hand. It lives
# inside Quartz.framework, which pyobjc-framework-Quartz already depends on.
_QUICKLOOK_UI = ("/System/Library/Frameworks/Quartz.framework"
                 "/Frameworks/QuickLookUI.framework")
_ql_namespace = {}
try:
    objc.loadBundle("QuickLookUI", _ql_namespace, bundle_path=_QUICKLOOK_UI)
    QLPreviewView = _ql_namespace["QLPreviewView"]
except (ImportError, KeyError, ValueError) as exc:          # pragma: no cover
    sys.exit(f"Could not load QuickLookUI ({exc}). Use --native instead.")

QL_STYLE_NORMAL = 0
STATUS_HEIGHT = 30.0
NS_KEY_DOWN = 10

HELP_TEXT = """\
Right / Down      next image
Left / Up         previous image
Space or F        toggle favorite
1 / 2 / 3         view all / favorites / non-favorites
                  (re-press 2 or 3 to refresh after toggling)
O                 open in Preview.app
Home / End        first / last image
E                 export favorites.txt / non_favorites.txt
G                 toggle fullscreen
H or ?            toggle this help
Q / Escape        quit

Zoom and scroll belong to Quick Look here: pinch, double-tap, and
two-finger scroll all work, and we ask it to carry that state over
as you move between images.

This backend exists to compare Apple's renderer against --native
on your own files. --native is the default because it prefetches
neighbours, so arrowing through a burst stays instant."""


def image_pixel_size(path):
    """Read pixel dimensions from metadata, without decoding the image."""
    try:
        import Quartz
        source = Quartz.CGImageSourceCreateWithURL(
            NSURL.fileURLWithPath_(str(path)), None)
        if source is None:
            return None
        props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None) or {}
        width = props.get(Quartz.kCGImagePropertyPixelWidth)
        height = props.get(Quartz.kCGImagePropertyPixelHeight)
        if not width or not height:
            return None
        if props.get(Quartz.kCGImagePropertyOrientation, 1) >= 5:
            width, height = height, width       # sideways: report as displayed
        return int(width), int(height)
    except Exception:
        return None


class KeyWindow(NSWindow):
    """Window that gets first refusal on key events.

    Quick Look's view consumes arrow keys for its own scrolling, so hooking
    keyDown_ on a view never sees them. Intercepting in sendEvent_ puts our
    culling keys ahead of the whole responder chain.
    """

    def initWithContentRect_styleMask_backing_defer_(self, rect, mask,
                                                     backing, defer):
        self = objc.super(KeyWindow, self).\
            initWithContentRect_styleMask_backing_defer_(rect, mask, backing,
                                                        defer)
        if self is not None:
            self.controller = None
        return self

    def sendEvent_(self, event):
        if (event.type() == NS_KEY_DOWN and self.controller is not None
                and self.controller.handle_key(event)):
            return
        objc.super(KeyWindow, self).sendEvent_(event)


class WindowDelegate(NSObject):
    """AppKit delegates must be Objective-C objects, so the plain-Python
    controller can't serve as one itself."""

    def initWithController_(self, controller):
        self = objc.super(WindowDelegate, self).init()
        if self is not None:
            self.controller = controller
        return self

    def windowWillClose_(self, notification):
        if self.controller is not None:
            self.controller.shutdown()


class QuickLookViewer:
    """Culling shell around QLPreviewView."""

    def __init__(self, directory, files, store, initial_filter="all"):
        self.directory = Path(directory)
        self.all_files = files
        self.store = store
        self.filter_mode = initial_filter
        self.view = apply_filter(files, store.favorites, initial_filter,
                                 self.directory)
        self.index = 0
        self.show_help = False
        self.save_warning = None
        self._flash_text = None
        self._display_state = None      # Quick Look's zoom/scroll, carried over
        self._build_window()
        self.show_current()

    # -- window -------------------------------------------------------------

    def _build_window(self):
        screen = NSScreen.mainScreen().frame()
        width = min(1600.0, screen.size.width * 0.9)
        height = min(1000.0, screen.size.height * 0.9)
        rect = NSMakeRect((screen.size.width - width) / 2,
                          (screen.size.height - height) / 2, width, height)
        mask = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = KeyWindow.alloc().\
            initWithContentRect_styleMask_backing_defer_(
                rect, mask, NSBackingStoreBuffered, False)
        self.window.setTitle_(f"PhotoViewer (Quick Look) — {self.directory}")
        # Held on self as well as on the window: NSWindow does not retain its
        # delegate, so letting it go out of scope would crash on close.
        self._delegate = WindowDelegate.alloc().initWithController_(self)
        self.window.setDelegate_(self._delegate)
        self.window.controller = self

        content = self.window.contentView()
        cw = content.frame().size.width
        ch = content.frame().size.height

        self.preview = QLPreviewView.alloc().initWithFrame_style_(
            NSMakeRect(0, STATUS_HEIGHT, cw, ch - STATUS_HEIGHT),
            QL_STYLE_NORMAL)
        # We drive the lifecycle ourselves so navigation can reuse one view.
        self.preview.setShouldCloseWithWindow_(False)
        self.preview.setAutostarts_(True)
        self.preview.setAutoresizingMask_(NSViewWidthSizable
                                          | NSViewHeightSizable)
        content.addSubview_(self.preview)

        self.status = self._label(NSMakeRect(8, 6, cw - 16, 18), 12,
                                  NSColor.colorWithCalibratedWhite_alpha_(
                                      0.87, 1.0))
        self.status.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(self.status)

        self.help = self._label(
            NSMakeRect(cw / 2 - 300, ch / 2 - 250, 600, 480), 13,
            NSColor.colorWithCalibratedWhite_alpha_(0.93, 1.0))
        self.help.setFont_(NSFont.userFixedPitchFontOfSize_(12))
        self.help.setStringValue_(HELP_TEXT)
        self.help.setDrawsBackground_(True)
        self.help.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.88))
        self.help.setAutoresizingMask_(NSViewMinYMargin | NSViewMaxYMargin)
        self.help.setHidden_(True)
        content.addSubview_positioned_relativeTo_(self.help, 1, None)

        self.window.makeKeyAndOrderFront_(None)

    def _label(self, rect, size, color):
        field = NSTextField.alloc().initWithFrame_(rect)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setFont_(NSFont.systemFontOfSize_(size))
        field.setTextColor_(color)
        return field

    # -- navigation ---------------------------------------------------------

    def current_file(self):
        if not self.view:
            return None
        self.index = max(0, min(self.index, len(self.view) - 1))
        return self.view[self.index]

    def step(self, delta):
        if not self.view:
            return
        new = max(0, min(self.index + delta, len(self.view) - 1))
        if new != self.index:
            self.index = new
            self.show_current()

    def jump(self, index):
        if self.view:
            self.index = max(0, min(index, len(self.view) - 1))
            self.show_current()

    def show_current(self):
        current = self.current_file()
        if current is None:
            self.preview.setPreviewItem_(None)
            self.update_chrome()
            return
        # Remember Quick Look's zoom/scroll and hand it back for the next
        # image, so checking the same spot across a burst still works.
        state = self.preview.displayState()
        if state is not None:
            self._display_state = state
        self.preview.setPreviewItem_(NSURL.fileURLWithPath_(str(current)))
        if self._display_state is not None:
            try:
                self.preview.setDisplayState_(self._display_state)
            except Exception:
                pass        # display state is opaque; never fail navigation
        self.update_chrome()

    # -- favorites / filters ------------------------------------------------

    def toggle_favorite(self):
        current = self.current_file()
        if current is None:
            return
        name = rel_name(current, self.directory)
        try:
            now_fav = self.store.toggle(name)
            self.save_warning = None
        except OSError as exc:
            now_fav = self.store.is_favorite(name)
            self.save_warning = (f"⚠ FAVORITES NOT SAVED ({exc}) — fix the "
                                 "folder permissions/disk, then toggle any "
                                 "favorite to retry")
        self.flash(("★ Favorited" if now_fav else "☆ Removed favorite")
                   + f"  {current.name}")
        self.update_chrome()

    def set_filter(self, mode):
        current = self.current_file()
        self.filter_mode = mode
        self.view = apply_filter(self.all_files, self.store.favorites, mode,
                                 self.directory)
        self.index = pick_view_index(self.view, self.all_files, current)
        self.show_current()

    def export_lists(self):
        favs = apply_filter(self.all_files, self.store.favorites, "fav",
                            self.directory)
        others = apply_filter(self.all_files, self.store.favorites, "unfav",
                              self.directory)
        try:
            (self.directory / "favorites.txt").write_text(
                "\n".join(rel_name(p, self.directory) for p in favs) + "\n",
                encoding="utf-8")
            (self.directory / "non_favorites.txt").write_text(
                "\n".join(rel_name(p, self.directory) for p in others) + "\n",
                encoding="utf-8")
        except OSError as exc:
            self.flash(f"⚠ Export FAILED: {exc}")
            return
        self.flash(f"Exported {len(favs)} favorites → favorites.txt, "
                   f"{len(others)} others → non_favorites.txt")

    def open_in_preview(self):
        current = self.current_file()
        if current is None:
            return
        try:
            subprocess.Popen(["open", "-a", "Preview", str(current)])
        except OSError as exc:
            self.flash(f"⚠ Could not open Preview: {exc}")

    # -- chrome -------------------------------------------------------------

    def update_chrome(self):
        current = self.current_file()
        self.help.setHidden_(not self.show_help)
        nfav = len(self.store.favorites)
        view_label = FILTER_LABELS[self.filter_mode]
        if current is None:
            text = (f"View: {view_label}   0 images   ★ {nfav} favorites"
                    "   (1 for all images)")
        else:
            favorited = self.store.is_favorite(
                rel_name(current, self.directory))
            parts = [current.name, f"{self.index + 1} / {len(self.view)}",
                     "★" if favorited else "☆"]
            size = image_pixel_size(current)
            if size:
                parts.append(f"{size[0]}×{size[1]}")
            parts += ["Quick Look", f"View: {view_label}",
                      f"★ {nfav} favorites", "(h for help)"]
            text = "   ".join(parts)
        if self._flash_text:
            text = f"{self._flash_text}   |   {text}"
        if self.save_warning:
            text = f"{self.save_warning}   |   {text}"
        self.status.setStringValue_(text)
        self.status.setTextColor_(
            NSColor.systemOrangeColor() if self.save_warning
            else NSColor.colorWithCalibratedWhite_alpha_(0.87, 1.0))

    def flash(self, message):
        self._flash_text = message
        self.update_chrome()

    def toggle_help(self):
        self.show_help = not self.show_help
        self.update_chrome()

    # -- keys ---------------------------------------------------------------

    def handle_key(self, event):
        """Return True when the key was ours, so Quick Look never sees it."""
        chars = event.charactersIgnoringModifiers()
        char = (chars or "").lower()
        code = ord(chars[0]) if chars else 0
        self._flash_text = None

        if code in (0xF703, 0xF701):            # right / down
            self.step(1)
        elif code in (0xF702, 0xF700):          # left / up
            self.step(-1)
        elif code == 0xF729:                    # home
            self.jump(0)
        elif code == 0xF72B:                    # end
            self.jump(len(self.view) - 1)
        elif char in (" ", "f"):
            self.toggle_favorite()
        elif char == "1":
            self.set_filter("all")
        elif char == "2":
            self.set_filter("fav")
        elif char == "3":
            self.set_filter("unfav")
        elif char == "e":
            self.export_lists()
        elif char == "g":
            self.window.toggleFullScreen_(None)
        elif char in ("h", "?"):
            self.toggle_help()
        elif char == "o":
            self.open_in_preview()
        elif char == "q":
            self.window.close()
        elif code == 0x1B:                      # escape
            if self.show_help:
                self.toggle_help()
            else:
                self.window.close()
        else:
            return False        # not ours: let Quick Look zoom/scroll with it
        return True

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self):
        self.preview.close()
        NSApplication.sharedApplication().terminate_(None)


def run(directory, files, store, initial_filter="all"):
    """Entry point used by photo_viewer.py --quicklook."""
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    # Reachable from the window (window.controller), which AppKit retains, so
    # the run loop's callbacks always find it.
    QuickLookViewer(directory, files, store, initial_filter=initial_filter)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0
