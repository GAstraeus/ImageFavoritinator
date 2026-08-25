#!/usr/bin/env python3
"""
native_viewer.py — pixel-exact macOS backend for photo_viewer.py.

This is the "piggyback on Preview.app" answer. Preview isn't scriptable enough
to drive a culling session (no way to read which image is showing, no keys to
hook), but everything that makes it look good is public API, and this module
uses the same two pieces Preview does:

  * ImageIO / CGImageSource for decoding — Apple's own RAW pipeline, which
    lists com.canon.cr3-raw-image among its supported types. Same decoder,
    same colours as Preview, no rawpy needed.
  * NSScrollView magnification for zoom — the actual Cocoa zoom machinery,
    so pinch-to-zoom, smart-magnify double-tap, and momentum two-finger
    panning are the system's implementations rather than imitations.

The reason this backend exists at all: tkinter draws one image pixel per
*point*, so on a 2x Retina display a fit-to-window photo can only ever use
half the panel's pixels. Here the document view is sized in points
(pixels / backingScaleFactor), so magnification 1.0 puts exactly one image
pixel on one device pixel — the same sharpness Preview gives you.

Reached via: python3 photo_viewer.py /path/to/images --native
"""

import queue
import subprocess
import sys
from pathlib import Path

try:
    import objc
    import Quartz
    from AppKit import (
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSClipView,
        NSColor,
        NSEventModifierFlagCommand,
        NSEventModifierFlagOption,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMakeRect,
        NSScreen,
        NSScrollView,
        NSTextField,
        NSView,
        NSViewHeightSizable,
        NSViewMinYMargin,
        NSViewWidthSizable,
        NSWindow,
        NSWindowAbove,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
        NSGraphicsContext,
        NSParagraphStyleAttributeName,
    )
    from Foundation import (
        NSAttributedString,
        NSMutableParagraphStyle,
        NSNotificationCenter,
        NSTimer,
        NSURL,
    )
except ImportError as exc:                                  # pragma: no cover
    sys.exit(
        f"The --native backend needs PyObjC ({exc}). Install it with:\n"
        "    python3 -m pip install pyobjc-framework-Quartz\n"
        "Or drop --native to use the tkinter viewer."
    )

# PyObjC resolves framework symbols lazily, and that resolution is *not*
# thread-safe: objc._lazyimport.get_constant ends with funcmap.pop(name), so
# two worker threads reaching for the same symbol race and the loser gets a
# KeyError (seen roughly one decode in twenty). Bind every symbol we use here,
# on the main thread at import time, so the workers only ever touch resolved
# names.
CGImageSourceCreateWithURL = Quartz.CGImageSourceCreateWithURL
CGImageSourceCopyPropertiesAtIndex = Quartz.CGImageSourceCopyPropertiesAtIndex
CGImageSourceCreateThumbnailAtIndex = Quartz.CGImageSourceCreateThumbnailAtIndex
CGImageGetWidth = Quartz.CGImageGetWidth
CGImageGetHeight = Quartz.CGImageGetHeight
CGContextDrawImage = Quartz.CGContextDrawImage
CGContextFillRect = Quartz.CGContextFillRect
CGContextSetGrayFillColor = Quartz.CGContextSetGrayFillColor
CGContextSetInterpolationQuality = Quartz.CGContextSetInterpolationQuality
kCGImagePropertyOrientation = Quartz.kCGImagePropertyOrientation
kCGImagePropertyPixelHeight = Quartz.kCGImagePropertyPixelHeight
kCGImagePropertyPixelWidth = Quartz.kCGImagePropertyPixelWidth
kCGImageSourceCreateThumbnailFromImageAlways = \
    Quartz.kCGImageSourceCreateThumbnailFromImageAlways
kCGImageSourceCreateThumbnailWithTransform = \
    Quartz.kCGImageSourceCreateThumbnailWithTransform
kCGImageSourceShouldCacheImmediately = Quartz.kCGImageSourceShouldCacheImmediately
kCGImageSourceThumbnailMaxPixelSize = Quartz.kCGImageSourceThumbnailMaxPixelSize
kCGInterpolationDefault = Quartz.kCGInterpolationDefault
kCGInterpolationHigh = Quartz.kCGInterpolationHigh
kCGInterpolationNone = Quartz.kCGInterpolationNone

# Apple's RAW developing pipeline, used only for --raw-develop. Looked up here
# (main thread, import time) for the same thread-safety reason as above, and
# tolerated as missing so an older macOS still runs everything else.
try:
    CIRAWFilter = objc.lookUpClass("CIRAWFilter")
    CIContext = objc.lookUpClass("CIContext")
except objc.error:                                          # pragma: no cover
    CIRAWFilter = CIContext = None

from photo_viewer import (
    BROWSE_TIER,
    FULL_CACHE_SIZE,
    FULL_SETTLE_MS,
    FULL_TIER,
    MAX_ZOOM,
    MIN_ZOOM,
    PREFETCH_AHEAD,
    PREFETCH_BEHIND,
    RAW_EXTS,
    RELEVANCE_WINDOW,
    Decoded,
    FILTER_LABELS,
    ImageLoader,
    apply_filter,
    clamp,
    pick_view_index,
    rel_name,
)

# CGImages are 4 bytes/px where PIL images are 3, so the browse cache is a
# little smaller here to land in the same memory ballpark.
NATIVE_BROWSE_CACHE = 8
STATUS_HEIGHT = 30.0
ZOOM_STEP = 1.25

HELP_TEXT = """\
Right / Down      next image
Left / Up         previous image
Space or F        toggle favorite
1 / 2 / 3         view all / favorites / non-favorites
                  (re-press 2 or 3 to refresh after toggling)
0                 zoom to fit          9   zoom to 100% (pixel exact)
+ / -             zoom in / out
pinch             zoom, like Preview
two-finger scroll pan       Cmd/Opt-scroll  zoom at the pointer
double-tap        smart zoom
P                 smooth zoom (like Preview) <-> hard pixels
Home / End        first / last image
E                 export favorites.txt / non_favorites.txt
G                 toggle fullscreen
O                 open in Preview.app
H or ?            toggle this help
Q / Escape        quit

Zoom and position stay put as you arrow through images, so you can
check focus on the same spot across a burst."""


# ---------------------------------------------------------------------------
# Decoding via ImageIO — Apple's RAW pipeline, the one Preview.app uses
# ---------------------------------------------------------------------------

def decode_native(path, max_side=0):
    """Decode any file macOS can read. max_side 0/None means full resolution.

    Always goes through CGImageSourceCreateThumbnailAtIndex: with
    kCGImageSourceCreateThumbnailFromImageAlways and no maximum pixel size it
    returns the image at full size, and unlike
    CGImageSourceCreateImageAtIndex it applies the EXIF / RAW orientation for
    us, so one code path covers both tiers.

    Runs on a worker thread, hence the explicit autorelease pool: without one,
    every temporary Foundation object would be held until the thread exits,
    which for a session of thousands of raws means never.
    """
    with objc.autorelease_pool():
        url = NSURL.fileURLWithPath_(str(path))
        source = CGImageSourceCreateWithURL(url, None)
        if source is None:
            raise RuntimeError("macOS could not read this file")

        props = CGImageSourceCopyPropertiesAtIndex(source, 0, None) or {}
        orig_w = props.get(kCGImagePropertyPixelWidth) or 0
        orig_h = props.get(kCGImagePropertyPixelHeight) or 0
        if props.get(kCGImagePropertyOrientation, 1) >= 5:
            orig_w, orig_h = orig_h, orig_w     # sideways: report display size

        options = {
            kCGImageSourceCreateThumbnailFromImageAlways: True,
            kCGImageSourceCreateThumbnailWithTransform: True,
            # Rasterise here on the worker thread; otherwise CoreGraphics
            # decodes lazily on the main thread at first draw and every image
            # stutters as it appears.
            kCGImageSourceShouldCacheImmediately: True,
        }
        if max_side:
            options[kCGImageSourceThumbnailMaxPixelSize] = int(max_side)

        image = CGImageSourceCreateThumbnailAtIndex(source, 0, options)
        if image is None:
            raise RuntimeError(
                "macOS ImageIO could not decode this image "
                "(a very new camera model may need a macOS update)")
        return Decoded(image,
                       (max(orig_w, CGImageGetWidth(image)),
                        max(orig_h, CGImageGetHeight(image))),
                       BROWSE_TIER if max_side else FULL_TIER)


def decode_native_develop(path, max_side=0):
    """Like decode_native, but develops raw sensor data for the full tier.

    ImageIO may hand back the full-size JPEG the camera embedded in a raw file
    rather than developing the sensor data. That JPEG is fast and already
    sharpened, but its 8x8 compression blocks become visible under heavy zoom.
    CIRAWFilter is Apple's actual raw pipeline, so this trades speed for pixels
    with no compression history.

    Only the full tier pays the cost: browsing still uses the embedded preview.
    Falls back to decode_native for non-raw files and on any failure, so a
    camera Core Image doesn't know can never break a culling session.
    """
    if max_side or CIRAWFilter is None or \
            Path(path).suffix.lower() not in RAW_EXTS:
        return decode_native(path, max_side)

    try:
        with objc.autorelease_pool():
            url = NSURL.fileURLWithPath_(str(path))
            raw_filter = CIRAWFilter.alloc().initWithImageURL_(url)
            if raw_filter is None:
                return decode_native(path, max_side)
            ci_image = raw_filter.outputImage()
            if ci_image is None:
                return decode_native(path, max_side)
            extent = ci_image.extent()
            image = CIContext.context().createCGImage_fromRect_(ci_image,
                                                               extent)
            if image is None:
                return decode_native(path, max_side)
            return Decoded(image,
                           (CGImageGetWidth(image), CGImageGetHeight(image)),
                           FULL_TIER)
    except Exception:
        return decode_native(path, max_side)


# ---------------------------------------------------------------------------
# Pure geometry (no AppKit calls, so it is unit-testable)
# ---------------------------------------------------------------------------

def points_for(pixel_size, backing_scale):
    """Point size that puts one image pixel on one device pixel at 100%."""
    scale = backing_scale or 1.0
    return (max(1.0, pixel_size[0] / scale), max(1.0, pixel_size[1] / scale))


def fit_magnification(doc_size, clip_size):
    """Magnification that fits the whole document view in the viewport.

    Capped at 1.0: blowing a small image up past its real pixels by default
    would present mush as if it were detail.
    """
    dw, dh = doc_size
    cw, ch = clip_size
    if min(dw, dh, cw, ch) <= 0:
        return 1.0
    return min(cw / dw, ch / dh, 1.0)


def origin_for_center(center, doc_size, visible_size):
    """Scroll origin (document coords) that puts `center` in the middle.

    `center` is normalized 0..1 within the document view. Clamped so panning
    can never leave the image, and ignored on an axis that fully fits.
    """
    origin = []
    for norm, extent, visible in zip(center, doc_size, visible_size):
        if visible >= extent:
            origin.append((extent - visible) / 2.0)   # smaller than the view
        else:
            origin.append(clamp(norm * extent - visible / 2.0,
                                0.0, extent - visible))
    return tuple(origin)


def center_for_rect(visible_rect, doc_size):
    """Normalized centre of a visible rect, for restoring it on another image."""
    (x, y), (w, h) = visible_rect
    dw, dh = doc_size
    if min(dw, dh) <= 0:
        return (0.5, 0.5)
    return (clamp((x + w / 2) / dw, 0.0, 1.0),
            clamp((y + h / 2) / dh, 0.0, 1.0))


def interpolation_for(magnification, pixel_peep=False):
    """Pick a resampling filter, matching Preview.app by default.

    Preview smooths at every zoom level. Nearest-neighbour past 2x -- which is
    what this used to do -- shows hard blocky pixels, so it reads as
    "pixelated" even when no detail is missing at all. Smoothing is therefore
    the default. Nearest is still the honest choice for judging focus at the
    pixel level, so it stays reachable on the P key.
    """
    if pixel_peep and magnification > 1.0:
        return kCGInterpolationNone
    return kCGInterpolationHigh


# ---------------------------------------------------------------------------
# AppKit views
# ---------------------------------------------------------------------------

class CenteringClipView(NSClipView):
    """Keeps a document view smaller than the viewport centred, not cornered."""

    def constrainBoundsRect_(self, proposed):
        rect = objc.super(CenteringClipView, self).constrainBoundsRect_(proposed)
        document = self.documentView()
        if document is None:
            return rect
        frame = document.frame()
        if rect.size.width > frame.size.width:
            rect.origin.x = (frame.size.width - rect.size.width) / 2.0
        if rect.size.height > frame.size.height:
            rect.origin.y = (frame.size.height - rect.size.height) / 2.0
        return rect


class ImageCanvas(NSView):
    """Draws the current CGImage, and carries every AppKit callback.

    All the state and logic live on a plain Python controller; this class
    exists because timers, key events, and window delegation need a real
    Objective-C object to talk to.
    """

    def initWithFrame_(self, frame):
        self = objc.super(ImageCanvas, self).initWithFrame_(frame)
        if self is None:
            return None
        self.controller = None
        self.source = None       # Decoded, or None while loading
        self.message = None      # shown instead of an image
        return self

    # -- drawing ------------------------------------------------------------

    def isOpaque(self):
        return True

    def drawRect_(self, dirty):
        context = NSGraphicsContext.currentContext()
        ctx = context.CGContext() if hasattr(context, "CGContext") \
            else context.graphicsPort()
        CGContextSetGrayFillColor(ctx, 0.067, 1.0)   # #111111
        CGContextFillRect(ctx, dirty)

        if self.source is not None:
            scroll = self.enclosingScrollView()
            magnification = scroll.magnification() if scroll else 1.0
            controller = self.controller
            peep = controller.pixel_peep if controller is not None else False
            CGContextSetInterpolationQuality(
                ctx, interpolation_for(magnification, peep))
            # Draw the whole image into bounds and let CoreGraphics clip to
            # the dirty rect: it rasterises tile-wise, so a 45 MP frame at 4x
            # only ever paints what is on screen.
            CGContextDrawImage(ctx, self.bounds(), self.source.image)
        elif self.message:
            self._draw_message(self.message)

    @objc.python_method
    def _draw_message(self, message):
        style = NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(1)     # NSTextAlignmentCenter
        text = NSAttributedString.alloc().initWithString_attributes_(
            message, {
                NSFontAttributeName: NSFont.systemFontOfSize_(18),
                NSForegroundColorAttributeName: NSColor.grayColor(),
                NSParagraphStyleAttributeName: style,
            })
        bounds = self.bounds()
        size = text.size()
        text.drawInRect_(NSMakeRect(0, (bounds.size.height - size.height) / 2,
                                    bounds.size.width, size.height))

    # -- input --------------------------------------------------------------

    def acceptsFirstResponder(self):
        return True

    def keyDown_(self, event):
        if self.controller is None or not self.controller.handle_key(event):
            objc.super(ImageCanvas, self).keyDown_(event)

    def scrollWheel_(self, event):
        # Plain scrolling pans (the scroll view's job); with a modifier held it
        # zooms at the pointer, matching Preview and most image editors.
        modifiers = event.modifierFlags()
        if modifiers & (NSEventModifierFlagCommand | NSEventModifierFlagOption):
            delta = event.scrollingDeltaY() or event.deltaY()
            if delta and self.controller is not None:
                self.controller.zoom_by(
                    ZOOM_STEP if delta > 0 else 1 / ZOOM_STEP,
                    self.convertPoint_fromView_(event.locationInWindow(), None))
            return
        objc.super(ImageCanvas, self).scrollWheel_(event)

    # -- callbacks ----------------------------------------------------------

    def pollResults_(self, timer):
        if self.controller is not None:
            self.controller.poll_results()

    def loadFull_(self, timer):
        if self.controller is not None:
            self.controller.load_full()

    def clearFlash_(self, timer):
        if self.controller is not None:
            self.controller.clear_flash()

    def viewWillStartLiveMagnify(self):
        if self.controller is not None:
            self.controller.live_magnify = True

    def viewDidEndLiveMagnify(self):
        if self.controller is not None:
            self.controller.live_magnify = False
            self.controller.magnification_changed()

    def boundsDidChange_(self, notification):
        """The viewport moved — scroll wheel, scroller, trackpad, anything."""
        if self.controller is not None:
            self.controller.viewport_moved()

    def windowWillClose_(self, notification):
        if self.controller is not None:
            self.controller.quit()

    def windowDidResize_(self, notification):
        if self.controller is not None:
            self.controller.relayout()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class NativeViewer:
    """Culling state and behaviour; mirrors ViewerApp, drawn with AppKit."""

    def __init__(self, directory, files, store, loader, initial_filter="all"):
        self.directory = Path(directory)
        self.all_files = files
        self.store = store
        self.loader = loader
        self.filter_mode = initial_filter
        self.view = apply_filter(files, store.favorites, initial_filter,
                                 self.directory)
        self.index = 0
        self.direction = 1
        self.magnification = None      # None = fit to window
        self.center = (0.5, 0.5)
        self.save_warning = None
        self.show_help = False
        self._flash_text = None
        self._flash_timer = None
        self._full_timer = None
        self._detail_note = ""
        self.backing_scale = 1.0
        self.live_magnify = False
        # Off by default so magnifying looks like Preview (smooth). On, it
        # switches to nearest-neighbour so you can judge focus on real pixels.
        self.pixel_peep = False
        # True while we are programmatically restoring zoom/position, so the
        # bounds-changed notifications that causes don't overwrite the
        # position we are in the middle of restoring.
        self._restoring = False
        loader.is_relevant = self._is_relevant

        self._build_window()
        self.show_current()

    # -- window -------------------------------------------------------------

    def _build_window(self):
        screen = NSScreen.mainScreen()
        visible = screen.visibleFrame()
        self.backing_scale = float(screen.backingScaleFactor() or 1.0)
        width = visible.size.width * 0.9
        height = visible.size.height * 0.9
        frame = NSMakeRect(visible.origin.x + (visible.size.width - width) / 2,
                           visible.origin.y + (visible.size.height - height) / 2,
                           width, height)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_(f"PhotoViewer (native) — {self.directory}")
        self.window.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.067, 1.0))

        content = self.window.contentView()
        cw = content.frame().size.width
        ch = content.frame().size.height

        self.canvas = ImageCanvas.alloc().initWithFrame_(
            NSMakeRect(0, 0, cw, ch - STATUS_HEIGHT))
        self.canvas.controller = self

        self.scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, STATUS_HEIGHT, cw, ch - STATUS_HEIGHT))
        clip = CenteringClipView.alloc().initWithFrame_(self.scroll.bounds())
        clip.setDrawsBackground_(True)
        clip.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.067, 1.0))
        self.scroll.setContentView_(clip)
        self.scroll.setDocumentView_(self.canvas)
        # Remember where the user panned to, so navigating to the next photo
        # (and the full-resolution copy arriving) keeps the same spot on screen.
        clip.setPostsBoundsChangedNotifications_(True)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self.canvas, "boundsDidChange:",
            "NSViewBoundsDidChangeNotification", clip)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setHasHorizontalScroller_(True)
        self.scroll.setAllowsMagnification_(True)   # pinch + smart magnify
        self.scroll.setMinMagnification_(MIN_ZOOM)
        self.scroll.setMaxMagnification_(MAX_ZOOM)
        self.scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.scroll.setDrawsBackground_(True)
        self.scroll.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.067, 1.0))
        content.addSubview_(self.scroll)

        self.status = self._label(NSMakeRect(8, 4, cw - 16, STATUS_HEIGHT - 8),
                                  13, NSColor.lightGrayColor())
        self.status.setAutoresizingMask_(NSViewWidthSizable)
        content.addSubview_(self.status)

        # Floating overlays: siblings of the scroll view, so they neither
        # scroll nor zoom with the photo.
        self.star = self._label(NSMakeRect(18, ch - 62, 70, 50), 40,
                                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                                    1.0, 0.8, 0.2, 1.0))
        self.star.setAutoresizingMask_(NSViewMinYMargin)
        content.addSubview_positioned_relativeTo_(self.star, NSWindowAbove,
                                                  self.scroll)

        self.help = self._label(NSMakeRect(cw / 2 - 300, ch / 2 - 230, 600, 440),
                                12.5, NSColor.whiteColor())
        self.help.setFont_(NSFont.userFixedPitchFontOfSize_(12.5))
        self.help.setDrawsBackground_(True)
        self.help.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.85))
        self.help.setStringValue_(HELP_TEXT)
        self.help.setHidden_(True)
        content.addSubview_positioned_relativeTo_(self.help, NSWindowAbove,
                                                  self.scroll)

        self.window.setDelegate_(self.canvas)
        self.window.makeFirstResponder_(self.canvas)
        self.window.makeKeyAndOrderFront_(None)

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.025, self.canvas, "pollResults:", None, True)

    def _label(self, frame, size, color):
        field = NSTextField.alloc().initWithFrame_(frame)
        field.setStringValue_("")
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setFont_(NSFont.systemFontOfSize_(size))
        field.setTextColor_(color)
        return field

    def relayout(self):
        content = self.window.contentView()
        cw = content.frame().size.width
        ch = content.frame().size.height
        self.help.setFrame_(NSMakeRect(cw / 2 - 300, ch / 2 - 230, 600, 440))
        if self.magnification is None:
            self._apply_zoom()      # fit view: a resize changes what fits
        self.update_chrome()        # the zoom percentage just moved

    # -- navigation ---------------------------------------------------------

    def current_file(self):
        if not self.view:
            return None
        self.index = max(0, min(self.index, len(self.view) - 1))
        return self.view[self.index]

    def step(self, delta):
        if not self.view:
            return
        self.direction = 1 if delta > 0 else -1
        new = max(0, min(self.index + delta, len(self.view) - 1))
        if new != self.index:
            self.index = new
            self.show_current()

    def jump(self, index):
        if not self.view:
            return
        self.index = max(0, min(index, len(self.view) - 1))
        self.show_current()

    def _is_relevant(self, path, tier):
        view, idx = self.view, self.index
        try:
            offset = abs(view.index(Path(path)) - idx)
        except ValueError:
            return False
        return offset == 0 if tier == FULL_TIER else offset <= RELEVANCE_WINDOW

    def show_current(self):
        current = self.current_file()
        if current is not None:
            self.loader.request(current, BROWSE_TIER)
            self.prefetch()
            self.schedule_full_load()
        self.render()

    def prefetch(self):
        ahead = PREFETCH_AHEAD if self.direction > 0 else PREFETCH_BEHIND
        behind = PREFETCH_BEHIND if self.direction > 0 else PREFETCH_AHEAD
        for offset in list(range(1, ahead + 1)) + \
                      [-o for o in range(1, behind + 1)]:
            i = self.index + offset
            if 0 <= i < len(self.view):
                self.loader.request(self.view[i], BROWSE_TIER)

    def schedule_full_load(self):
        if self._full_timer is not None:
            self._full_timer.invalidate()
        self._full_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            FULL_SETTLE_MS / 1000.0, self.canvas, "loadFull:", None, False)

    def load_full(self):
        self._full_timer = None
        current = self.current_file()
        if current is None:
            return
        self.loader.drop_full_except([current])
        self.loader.request(current, FULL_TIER)

    def poll_results(self):
        redraw = False
        try:
            while True:
                key, decoded, error = self.loader.results.get_nowait()
                self.loader.store(key, decoded if decoded is not None
                                  else (error or "decode failed"))
                current = self.current_file()
                if current is not None and key[0] == str(current):
                    redraw = True
        except queue.Empty:
            pass
        if redraw:
            self.render()

    # -- favorites / filtering ----------------------------------------------

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
            return
        self.flash(f"Opened {current.name} in Preview")

    # -- zoom ---------------------------------------------------------------

    def _doc_size(self):
        frame = self.canvas.frame().size
        return (frame.width, frame.height)

    def _clip_size(self):
        frame = self.scroll.contentView().frame().size
        return (frame.width, frame.height)

    def fit_magnification(self):
        return fit_magnification(self._doc_size(), self._clip_size())

    def zoom_to_fit(self):
        self.magnification = None
        self.center = (0.5, 0.5)
        self._apply_zoom()
        self.update_chrome()

    def zoom_to_actual(self):
        self.set_zoom(1.0)

    def zoom_by(self, factor, anchor=None):
        self.set_zoom(self.scroll.magnification() * factor, anchor)

    def set_zoom(self, magnification, anchor=None):
        fit = self.fit_magnification()
        target = clamp(magnification, max(MIN_ZOOM, fit * 0.5), MAX_ZOOM)
        # Zooming back out past fit snaps to fit, so "fit" stays reachable.
        self.magnification = None if target <= fit else target
        if anchor is not None:
            self.scroll.setMagnification_centeredAtPoint_(
                fit if self.magnification is None else target, anchor)
            self._remember_center()
            self.update_chrome()
            return
        self._apply_zoom()
        self.update_chrome()

    def _apply_zoom(self):
        """Push self.magnification / self.center onto the scroll view."""
        if self.live_magnify:
            return          # the user's fingers win over a background load
        fit = self.fit_magnification()
        was_restoring, self._restoring = self._restoring, True
        try:
            self.scroll.setMagnification_(
                fit if self.magnification is None else self.magnification)
            clip = self.scroll.contentView()
            visible = clip.bounds().size
            origin = origin_for_center(self.center, self._doc_size(),
                                       (visible.width, visible.height))
            clip.scrollToPoint_(origin)
            self.scroll.reflectScrolledClipView_(clip)
        finally:
            self._restoring = was_restoring

    def _remember_center(self):
        rect = self.scroll.contentView().bounds()
        self.center = center_for_rect(
            ((rect.origin.x, rect.origin.y), (rect.size.width, rect.size.height)),
            self._doc_size())

    def viewport_moved(self):
        """The user panned (wheel, scroller, trackpad) — record where to."""
        if self._restoring:
            return
        self._remember_center()

    def magnification_changed(self):
        """After a pinch: adopt what the user landed on."""
        current = self.scroll.magnification()
        fit = self.fit_magnification()
        self.magnification = None if current <= fit + 1e-6 else current
        self._remember_center()
        self.update_chrome()

    # -- rendering ----------------------------------------------------------

    def render(self):
        # Every frame change in here scrolls the viewport as a side effect, and
        # a bounds-changed notification arriving mid-render would record that
        # accidental position as the user's. Suppress the observer throughout,
        # not just around the zoom restore: resizing the canvas down to the
        # viewport (the "Loading …" branches below) would otherwise reset the
        # remembered centre to the middle of the frame, so arrowing onto an
        # image that hasn't decoded yet would throw away your zoom position —
        # exactly when stepping through a burst quickly.
        was_restoring, self._restoring = self._restoring, True
        try:
            self._render()
        finally:
            self._restoring = was_restoring

    def _render(self):
        current = self.current_file()
        canvas = self.canvas
        if current is None:
            canvas.source = None
            canvas.message = (
                f"No images in this view ({FILTER_LABELS[self.filter_mode]}).\n"
                "Press 1 to see all images.")
            canvas.setFrameSize_(self._clip_size())
            canvas.setNeedsDisplay_(True)
            self.update_chrome()
            return

        source, error = self._source_for(current)
        if source is None:
            # Keep whatever is on screen if we already have pixels for it;
            # only clear when there is genuinely nothing to show.
            canvas.source = None
            canvas.message = (f"Could not load {current.name}\n\n{error}"
                              if error else f"Loading {current.name} …")
            canvas.setFrameSize_(self._clip_size())
            canvas.setNeedsDisplay_(True)
            self.update_chrome()
            return

        canvas.message = None
        canvas.source = source
        pixels = (CGImageGetWidth(source.image), CGImageGetHeight(source.image))
        # Frame in points comes from the *original* size, so a browse preview
        # and the full-resolution copy occupy exactly the same geometry and
        # swapping one for the other never moves the image or changes zoom.
        canvas.setFrameSize_(points_for(source.orig_size, self.backing_scale))
        canvas.setNeedsDisplay_(True)
        self._apply_zoom()

        if max(pixels) < max(source.orig_size):
            self._detail_note = ("preview — loading full res…"
                                 if self.scroll.magnification() > 0.5
                                 else "preview")
        else:
            self._detail_note = "full res"
        self.update_chrome()

    def _source_for(self, path):
        error = None
        for tier in (FULL_TIER, BROWSE_TIER):
            entry = self.loader.get_cached(path, tier)
            if isinstance(entry, Decoded):
                return entry, None
            if isinstance(entry, str) and error is None:
                error = entry
        return None, error

    def update_chrome(self):
        current = self.current_file()
        favorited = current is not None and self.store.is_favorite(
            rel_name(current, self.directory))
        self.star.setHidden_(not favorited)
        self.help.setHidden_(not self.show_help)

        nfav = len(self.store.favorites)
        view_label = FILTER_LABELS[self.filter_mode]
        if current is None:
            text = f"View: {view_label}   0 images   ★ {nfav} favorites"
        else:
            ow, oh = 0, 0
            if self.canvas.source is not None:
                ow, oh = self.canvas.source.orig_size
            percent = self.scroll.magnification() * 100
            label = f"{percent:.0f}%" + (" (fit)" if self.magnification is None
                                         else "")
            parts = [current.name, f"{self.index + 1} / {len(self.view)}",
                     "★" if favorited else "☆"]
            if ow:
                parts += [f"{ow}×{oh}", label]
            if self._detail_note:
                parts.append(self._detail_note)
            if self.pixel_peep:
                parts.append("hard pixels")
            parts += [f"View: {view_label}", f"★ {nfav} favorites",
                      "(h for help)"]
            text = "   ".join(parts)
        if self._flash_text:
            text = f"{self._flash_text}   |   {text}"
        if self.save_warning:
            text = f"{self.save_warning}   |   {text}"
        self.status.setStringValue_(text)
        self.status.setTextColor_(
            NSColor.orangeColor() if self.save_warning
            else NSColor.lightGrayColor())

    def flash(self, message):
        if self._flash_timer is not None:
            self._flash_timer.invalidate()
        self._flash_text = message
        self._flash_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self.canvas, "clearFlash:", None, False)
        self.update_chrome()

    def clear_flash(self):
        self._flash_text = None
        self._flash_timer = None
        self.update_chrome()

    def toggle_help(self):
        self.show_help = not self.show_help
        self.update_chrome()

    def toggle_pixel_peep(self):
        self.pixel_peep = not self.pixel_peep
        self.canvas.setNeedsDisplay_(True)
        self.flash("Pixel peeping: hard pixels" if self.pixel_peep
                   else "Smooth zoom (like Preview)")
        self.update_chrome()

    # -- keys ---------------------------------------------------------------

    def handle_key(self, event):
        """Return True when the key was ours, so AppKit doesn't beep."""
        chars = event.charactersIgnoringModifiers()
        if not chars:
            return False
        code = ord(chars[0])
        char = chars[0].lower()
        command = bool(event.modifierFlags() & NSEventModifierFlagCommand)

        if code in (0xF703, 0xF701):        # right, down
            self.step(1)
        elif code in (0xF702, 0xF700):      # left, up
            self.step(-1)
        elif code == 0xF729:                # home
            self.jump(0)
        elif code == 0xF72B:                # end
            self.jump(len(self.view) - 1)
        elif char in (" ", "f"):
            self.toggle_favorite()
        elif char == "1" and not command:
            self.set_filter("all")
        elif char == "2":
            self.set_filter("fav")
        elif char == "3":
            self.set_filter("unfav")
        elif char == "0":                   # bare or with ⌘, as in Preview
            self.zoom_to_fit()
        elif char == "9" or (char == "1" and command):
            self.zoom_to_actual()
        elif char in ("+", "="):
            self.zoom_by(ZOOM_STEP)
        elif char in ("-", "_"):
            self.zoom_by(1 / ZOOM_STEP)
        elif char == "p":
            self.toggle_pixel_peep()
        elif char == "e":
            self.export_lists()
        elif char == "g":
            self.window.toggleFullScreen_(None)
        elif char == "h" or char == "?":
            self.toggle_help()
        elif char == "o":
            self.open_in_preview()
        elif char == "q":
            self.window.close()
        elif code == 0x1B:                  # escape
            if self.show_help:
                self.toggle_help()
            else:
                self.window.close()
        else:
            return False
        return True

    def quit(self):
        if self.save_warning:
            try:
                self.store.save()
            except OSError as exc:
                print(f"WARNING: favorites could not be saved to "
                      f"{self.store.path}: {exc}", file=sys.stderr)
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        NSNotificationCenter.defaultCenter().removeObserver_(self.canvas)
        self.loader.shutdown()
        NSApp().terminate_(None)


def run(directory, files, store, initial_filter="all", browse_side=None,
        raw_develop=False):
    """Entry point used by photo_viewer.py --native."""
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    if browse_side is None:
        screen = NSScreen.mainScreen().frame().size
        # Points are what a fit view can show; the margin leaves headroom for
        # a little zooming before the full-resolution copy lands.
        browse_side = max(2048, int(max(screen.width, screen.height) * 1.4))

    loader = ImageLoader(browse_side=browse_side,
                         browse_cache=NATIVE_BROWSE_CACHE,
                         full_cache=FULL_CACHE_SIZE,
                         decoder=decode_native_develop if raw_develop
                         else decode_native)
    # The controller outlives this local: it is reachable from the canvas
    # (canvas.controller), which AppKit retains as the scroll view's document
    # view, so the run loop's callbacks always find it.
    NativeViewer(directory, files, store, loader,
                 initial_filter=initial_filter)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0
