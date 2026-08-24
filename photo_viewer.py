#!/usr/bin/env python3
"""
photo_viewer.py — fast keyboard-driven culling tool for Canon CR3 raw images
(also works with JPEG / PNG / TIFF).

Usage:
    python3 photo_viewer.py /path/to/images
    python3 photo_viewer.py /path/to/images --native   # crisp Retina backend

Keys:
    Right / Down        next image
    Left / Up           previous image
    Space or F          toggle favorite on the current image
    1 / 2 / 3           view all / favorites only / non-favorites only
    0                   zoom to fit          (also Cmd-0)
    9                   zoom to 100%         (also Cmd-1)
    + / -               zoom in / out
    scroll wheel        zoom at the pointer
    drag                pan when zoomed in
    O                   open the current image in Preview.app
    Home / End          jump to first / last image in the current view
    E                   export favorites.txt and non_favorites.txt
    G                   toggle fullscreen
    H or ?              toggle help overlay
    Q / Escape          quit (Escape leaves fullscreen first)

Favorites are saved to photoviewer_favorites.json in the image directory
after every change, so it is always safe to quit.

Two resolution tiers are used: a screen-sized preview for instant browsing,
and the full-resolution image, loaded in the background as soon as you settle
on a photo, so zooming shows real pixels rather than an upscaled preview.

CR3 decoding uses the embedded full-size JPEG preview via rawpy (pip install
rawpy) when available, and falls back to macOS's built-in `sips`, which needs
nothing installed. Run --probe FILE.CR3 to see exactly what each path yields.

Note on sharpness: tkinter draws one image pixel per *point*, so on a Retina
display it cannot use the panel's full pixel grid — a fit-to-window photo is
drawn at half the panel's resolution. Use --native (pip install
pyobjc-framework-Quartz) for a pixel-exact Retina viewer with Preview-style
pinch/scroll zoom, or press O to open the current shot in Preview.app.
"""

import argparse
import io
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict, namedtuple
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit(
        "Pillow is required. Install it with:\n"
        "    python3 -m pip install pillow\n"
        "(and for fast CR3 decoding: python3 -m pip install rawpy)"
    )

try:
    import rawpy
    HAVE_RAWPY = True
except ImportError:
    rawpy = None
    HAVE_RAWPY = False

RAW_EXTS = {".cr3", ".cr2", ".crw"}
PLAIN_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
ALL_EXTS = RAW_EXTS | PLAIN_EXTS

FAVORITES_FILENAME = "photoviewer_favorites.json"

# Browse tier: previews sized for the screen, so flipping through images is
# instant and a dozen of them stay well under a few hundred MB of RAM.
# Full tier: the image at its real pixel size, for zooming. Derived from the
# screen at startup; this is only the fallback when there is no display info.
FALLBACK_BROWSE_SIDE = 2560
BROWSE_CACHE_SIZE = 12
FULL_CACHE_SIZE = 2
PREFETCH_AHEAD = 3
PREFETCH_BEHIND = 1
# Workers skip decodes for images no longer within this many positions of the
# current one (stale requests from holding down an arrow key).
RELEVANCE_WINDOW = 8
# Full-resolution loading waits this long after the last keypress, so holding
# down an arrow key doesn't queue a full decode for every image flown past.
FULL_SETTLE_MS = 350

BROWSE_TIER = "browse"
FULL_TIER = "full"

ZOOM_STEP = 1.25
MAX_ZOOM = 8.0
MIN_ZOOM = 0.02

# What a decoder hands back: the pixels we have, plus the true size of the
# original, so the viewer knows how much detail exists beyond this copy.
Decoded = namedtuple("Decoded", "image orig_size tier")


# ---------------------------------------------------------------------------
# Pure helpers (no GUI) — kept free of tkinter so they are unit-testable.
# ---------------------------------------------------------------------------

def natural_key(name):
    """Sort key that orders img2 before img10."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", name)]


def scan_images(directory, recursive=False):
    """Return image files under `directory`, sorted naturally by name."""
    directory = Path(directory)
    walker = directory.rglob("*") if recursive else directory.iterdir()
    files = [p for p in walker
             if p.is_file()
             and p.suffix.lower() in ALL_EXTS
             and not p.name.startswith(".")]
    return sorted(files, key=lambda p: natural_key(str(p.relative_to(directory))))


def apply_filter(files, favorite_names, mode, directory):
    """Filter a list of Paths by favorite status.  mode: all | fav | unfav."""
    if mode == "all":
        return list(files)
    is_fav = lambda p: rel_name(p, directory) in favorite_names
    if mode == "fav":
        return [p for p in files if is_fav(p)]
    if mode == "unfav":
        return [p for p in files if not is_fav(p)]
    raise ValueError(f"unknown filter mode: {mode}")


def pick_view_index(view, full_order, current):
    """Index to land on after switching views.

    Keeps the current file if it is still visible, otherwise lands on the
    nearest earlier file that is, so filtering doesn't jump somewhere random.
    """
    if not view:
        return 0
    try:
        return view.index(current)
    except ValueError:
        pass
    pos = {f: i for i, f in enumerate(full_order)}
    cur = pos.get(current, -1)
    best = 0
    for i, f in enumerate(view):
        if pos.get(f, -1) <= cur:
            best = i
        else:
            break
    return best


def rel_name(path, directory):
    """Name stored in the favorites file: path relative to the image dir."""
    return Path(path).relative_to(directory).as_posix()


def clamp(value, low, high):
    return low if value < low else (high if value > high else value)


class FavoritesStore:
    """Favorite filenames, persisted as JSON next to the images.

    Saves atomically (temp file + rename) after every change so a crash or
    force-quit never loses more than nothing.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / FAVORITES_FILENAME
        self.favorites = set()
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            favs = data.get("favorites", []) if isinstance(data, dict) else None
            if not isinstance(favs, list) or \
                    not all(isinstance(name, str) for name in favs):
                # Never proceed (and later save over) a file whose shape we
                # don't understand — a hand-edit could otherwise be destroyed.
                raise ValueError(
                    'expected {"favorites": ["file1.CR3", "file2.CR3", ...]}')
            self.favorites = set(favs)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            sys.exit(f"Could not read {self.path}: {exc}\n"
                     "Fix or move the file and try again.")

    def save(self):
        payload = json.dumps({"favorites": sorted(self.favorites)}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp",
                                   prefix=".favorites-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def is_favorite(self, name):
        return name in self.favorites

    def toggle(self, name):
        if name in self.favorites:
            self.favorites.discard(name)
        else:
            self.favorites.add(name)
        self.save()
        return name in self.favorites


# ---------------------------------------------------------------------------
# Image decoding
# ---------------------------------------------------------------------------

def _apply_libraw_flip(img, flip):
    """Apply LibRaw's orientation code (0/3/5/6) to a PIL image."""
    if flip == 3:
        return img.transpose(Image.Transpose.ROTATE_180)
    if flip == 5:
        return img.transpose(Image.Transpose.ROTATE_90)      # 90° CCW
    if flip == 6:
        return img.transpose(Image.Transpose.ROTATE_270)     # 90° CW
    return img


def _decode_raw_rawpy(path, want_full):
    """Decode a raw file with rawpy/LibRaw.

    Prefers the embedded full-size JPEG preview (fast, and what the camera
    itself shows). Falls back to demosaicing the sensor data when that preview
    is missing or is too small to count as full resolution.
    """
    with rawpy.imread(str(path)) as raw:
        sizes = raw.sizes
        flip = getattr(sizes, "flip", 0)
        # Dimensions of the developed image, in the camera's own orientation.
        full_w, full_h = sizes.iwidth, sizes.iheight
        if flip in (5, 6):
            full_w, full_h = full_h, full_w

        thumb = None
        try:
            thumb = raw.extract_thumb()
        except Exception:
            pass  # no embedded preview; fall through to a demosaic

        preview = None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            preview = Image.open(io.BytesIO(thumb.data))
            preview.load()
            orientation = preview.getexif().get(0x0112, 1)
            if orientation and orientation != 1:
                preview = ImageOps.exif_transpose(preview)
            elif flip:
                preview = _apply_libraw_flip(preview, flip)
        elif thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            preview = _apply_libraw_flip(Image.fromarray(thumb.data), flip)

        if preview is not None:
            longest = max(full_w, full_h) or 1
            # Canon embeds a full-size JPEG in CR3, so this is normally an
            # exact match; anything close enough is treated as full res.
            is_full_size = max(preview.size) >= 0.9 * longest
            if is_full_size:
                return preview, (max(preview.width, full_w),
                                 max(preview.height, full_h))
            if not want_full:
                # A small proxy is fine for browsing, but report the real size
                # so the viewer knows to fetch more detail before zooming.
                return preview, (full_w, full_h)

        # Either no preview at all, or the caller wants real full resolution
        # and the preview is only a small proxy: develop the sensor data.
        rgb = raw.postprocess(use_camera_wb=True, half_size=not want_full,
                              output_bps=8)
        img = Image.fromarray(rgb)   # postprocess already applies orientation
        return img, (max(img.width, full_w), max(img.height, full_h))


def _decode_raw_sips(path, max_side):
    """Decode a raw file with macOS's built-in sips (no pip installs needed).

    sips uses Apple's own RAW pipeline — the same one Preview.app uses.
    """
    fd, out = tempfile.mkstemp(suffix=".tiff")
    os.close(fd)
    try:
        cmd = ["sips", "-s", "format", "tiff"]
        if max_side:
            cmd += ["-Z", str(max_side)]
        cmd += [str(path), "--out", out]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"sips failed: {result.stderr.strip() or result.stdout.strip()}")
        img = Image.open(out)
        img.load()
        return img
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def decode_image(path, max_side=0):
    """Decode any supported image.

    max_side caps the longest edge (0 or None means full resolution).
    Returns a Decoded: the pixels, plus the original's true size so callers
    know whether more detail is available.
    """
    path = Path(path)
    ext = path.suffix.lower()
    want_full = not max_side

    if ext in RAW_EXTS:
        if HAVE_RAWPY:
            img, orig_size = _decode_raw_rawpy(path, want_full)
        elif sys.platform == "darwin":
            img = _decode_raw_sips(path, max_side)
            # Only the capped tier needs to ask how big the original was.
            orig_size = (_sips_dimensions(path) or img.size) if max_side \
                else img.size
        else:
            raise RuntimeError(
                "Raw decoding needs rawpy on this platform: "
                "python3 -m pip install rawpy")
    else:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)
        orig_size = img.size

    if max_side and (img.width > max_side or img.height > max_side):
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    orig_size = (max(orig_size[0], img.width), max(orig_size[1], img.height))
    return Decoded(img.convert("RGB"), orig_size,
                   BROWSE_TIER if max_side else FULL_TIER)


def _sips_dimensions(path):
    """Pixel size of an image according to sips, or None if it won't say."""
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True)
    except OSError:
        return None
    dims = {}
    for line in out.stdout.splitlines():
        key, _, value = line.strip().partition(":")
        if key in ("pixelWidth", "pixelHeight") and value.strip().isdigit():
            dims[key] = int(value.strip())
    if "pixelWidth" in dims and "pixelHeight" in dims:
        return dims["pixelWidth"], dims["pixelHeight"]
    return None


class ImageLoader:
    """Decodes images on worker threads with a per-tier LRU cache.

    tkinter is not thread-safe, so workers never touch the GUI: results land
    on a queue.Queue that the main thread drains from a `root.after` timer.
    Browse-tier requests are served before full-resolution ones, so a slow
    full decode never delays the next photo the user is flipping to.
    """

    def __init__(self, browse_side=FALLBACK_BROWSE_SIDE,
                 browse_cache=BROWSE_CACHE_SIZE, full_cache=FULL_CACHE_SIZE,
                 workers=2, is_relevant=None):
        self.browse_side = browse_side
        self.is_relevant = is_relevant or (lambda path, tier: True)
        self.results = queue.Queue()
        self._caches = {BROWSE_TIER: OrderedDict(), FULL_TIER: OrderedDict()}
        self._limits = {BROWSE_TIER: browse_cache, FULL_TIER: full_cache}
        self._pending = set()
        self._lock = threading.Lock()
        # Daemon threads (not ThreadPoolExecutor): executor workers are
        # non-daemon and get joined at interpreter exit, so quitting would
        # block for seconds on an in-flight sips/rawpy decode.
        self._tasks = queue.PriorityQueue()
        self._seq = itertools.count()
        self._workers = []
        for _ in range(workers):
            thread = threading.Thread(target=self._worker_loop, daemon=True)
            thread.start()
            self._workers.append(thread)

    def get_cached(self, path, tier=BROWSE_TIER):
        key = (str(path), tier)
        with self._lock:
            entry = self._caches[tier].get(key)
            if entry is not None:
                self._caches[tier].move_to_end(key)
            return entry

    def request(self, path, tier=BROWSE_TIER):
        """Ask for a decode if not already cached or in flight."""
        key = (str(path), tier)
        with self._lock:
            if key in self._caches[tier] or key in self._pending:
                return
            self._pending.add(key)
        priority = 0 if tier == BROWSE_TIER else 1
        self._tasks.put((priority, next(self._seq), str(path), tier))

    def _worker_loop(self):
        while True:
            _, _, path, tier = self._tasks.get()
            if path is None:
                return
            self._work(Path(path), tier)

    def _work(self, path, tier):
        key = (str(path), tier)
        try:
            if not self.is_relevant(path, tier):
                return  # stale request; skip the expensive decode
            try:
                max_side = self.browse_side if tier == BROWSE_TIER else 0
                self.results.put((key, decode_image(path, max_side), None))
            except Exception as exc:
                self.results.put((key, None, str(exc)))
        finally:
            with self._lock:
                self._pending.discard(key)

    def store(self, key, value):
        """Called by the main thread when draining the results queue."""
        tier = key[1]
        with self._lock:
            cache = self._caches[tier]
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > self._limits[tier]:
                cache.popitem(last=False)

    def drop_full_except(self, keep_paths):
        """Free full-resolution images the user has navigated away from."""
        keep = {str(p) for p in keep_paths}
        with self._lock:
            cache = self._caches[FULL_TIER]
            for key in [k for k in cache if k[0] not in keep]:
                del cache[key]

    def shutdown(self):
        for _ in self._workers:
            self._tasks.put((-1, next(self._seq), None, None))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

FILTER_LABELS = {"all": "All", "fav": "Favorites", "unfav": "Non-favorites"}

HELP_TEXT = """\
Right / Down      next image
Left / Up         previous image
Space or F        toggle favorite
1 / 2 / 3         view all / favorites / non-favorites
                  (re-press 2 or 3 to refresh after toggling)
0                 zoom to fit          9   zoom to 100%
+ / -             zoom in / out        scroll wheel  zoom at pointer
drag              pan when zoomed in
O                 open in Preview.app (true full-res pixels)
Home / End        first / last image
E                 export favorites.txt / non_favorites.txt
G                 toggle fullscreen
H or ?            toggle this help
Q / Escape        quit

Zoom and position stay put as you arrow through images, so you can
check focus on the same spot across a burst."""


class ViewerApp:
    def __init__(self, root, directory, files, store, loader,
                 initial_filter="all"):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.directory = Path(directory)
        self.all_files = files
        self.store = store
        self.loader = loader
        self.filter_mode = initial_filter
        self.view = apply_filter(files, store.favorites, initial_filter,
                                 self.directory)
        self.index = 0
        self.direction = 1
        self.show_help = False
        self.zoom = None            # None = fit to window, else image px/point
        self.center = (0.5, 0.5)    # normalized image point at canvas centre
        self.save_warning = None    # persistent banner when saving fails
        self._photo = None          # keep a reference or tk drops the image
        self._resize_job = None
        self._flash_text = None
        self._flash_job = None
        self._full_job = None
        self._drag_origin = None
        self._geom = None           # last drawn geometry, for zoom/pan math
        self._detail_note = ""      # "preview" / "full res" shown in status

        root.title(f"PhotoViewer — {self.directory}")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{int(sw * 0.9)}x{int(sh * 0.85)}+40+40")
        root.configure(bg="#111111")

        self.canvas = tk.Canvas(root, bg="#111111", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.status = tk.Label(root, anchor="w", bg="#1c1c1c", fg="#dddddd",
                               font=("Menlo", 13), padx=10, pady=6)
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

        for key in ("<Right>", "<Down>"):
            root.bind(key, lambda e: self.step(1))
        for key in ("<Left>", "<Up>"):
            root.bind(key, lambda e: self.step(-1))
        root.bind("<space>", lambda e: self.toggle_favorite())
        root.bind("f", lambda e: self.toggle_favorite())
        root.bind("F", lambda e: self.toggle_favorite())
        root.bind("1", lambda e: self.set_filter("all"))
        root.bind("2", lambda e: self.set_filter("fav"))
        root.bind("3", lambda e: self.set_filter("unfav"))
        root.bind("<Home>", lambda e: self.jump(0))
        root.bind("<End>", lambda e: self.jump(len(self.view) - 1))
        root.bind("e", lambda e: self.export_lists())
        root.bind("E", lambda e: self.export_lists())
        root.bind("g", lambda e: self.toggle_fullscreen())
        root.bind("G", lambda e: self.toggle_fullscreen())
        root.bind("h", lambda e: self.toggle_help())
        root.bind("H", lambda e: self.toggle_help())
        root.bind("?", lambda e: self.toggle_help())
        root.bind("o", lambda e: self.open_in_preview())
        root.bind("O", lambda e: self.open_in_preview())
        root.bind("<Command-o>", lambda e: self.open_in_preview())
        root.bind("q", lambda e: self.quit())
        root.bind("Q", lambda e: self.quit())
        root.bind("<Escape>", self.on_escape)
        # Zoom: 0 fits, 9 shows actual pixels; Cmd-0/Cmd-1 match Preview.app.
        root.bind("0", lambda e: self.zoom_to_fit())
        root.bind("<Command-0>", lambda e: self.zoom_to_fit())
        root.bind("9", lambda e: self.zoom_to_actual())
        root.bind("<Command-Key-1>", lambda e: self.zoom_to_actual())
        for key in ("<plus>", "<equal>", "<KP_Add>"):
            root.bind(key, lambda e: self.zoom_by(ZOOM_STEP))
        for key in ("<minus>", "<underscore>", "<KP_Subtract>"):
            root.bind(key, lambda e: self.zoom_by(1 / ZOOM_STEP))
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle_zoom(e))
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.canvas.bind("<Configure>", self.on_resize)

        self.loader.is_relevant = self._is_relevant
        root.after(25, self.poll_results)
        root.after(50, self.show_current)

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
        """Workers call this (from their thread) to skip stale decodes."""
        view, idx = self.view, self.index
        try:
            offset = abs(view.index(Path(path)) - idx)
        except ValueError:
            return False
        # A full decode is expensive and only ever wanted for the photo on
        # screen; previews are worth keeping for the neighbourhood.
        return offset == 0 if tier == FULL_TIER else offset <= RELEVANCE_WINDOW

    # -- favorites / filtering ----------------------------------------------

    def toggle_favorite(self):
        current = self.current_file()
        if current is None:
            return
        name = rel_name(current, self.directory)
        try:
            now_fav = self.store.toggle(name)
            self.save_warning = None    # a successful save persists everything
        except OSError as exc:
            # The in-memory toggle already happened; keep it (a later
            # successful save writes the whole set) but tell the user loudly
            # instead of letting tkinter swallow the traceback.
            now_fav = self.store.is_favorite(name)
            self.save_warning = (f"⚠ FAVORITES NOT SAVED ({exc}) — "
                                 "fix the folder permissions/disk, then "
                                 "toggle any favorite to retry")
        self.flash(("★ Favorited" if now_fav else "☆ Removed favorite")
                   + f"  {current.name}")
        self.render()

    def set_filter(self, mode):
        # No early-return when mode is unchanged: re-pressing the view's key
        # refreshes membership after favorites were toggled inside the view.
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
        fav_path = self.directory / "favorites.txt"
        other_path = self.directory / "non_favorites.txt"
        try:
            fav_path.write_text(
                "\n".join(rel_name(p, self.directory) for p in favs) + "\n",
                encoding="utf-8")
            other_path.write_text(
                "\n".join(rel_name(p, self.directory) for p in others) + "\n",
                encoding="utf-8")
        except OSError as exc:
            self.flash(f"⚠ Export FAILED: {exc}")
            return
        self.flash(f"Exported {len(favs)} favorites → favorites.txt, "
                   f"{len(others)} others → non_favorites.txt")

    def open_in_preview(self):
        """Hand the current file to Preview.app for pixel-exact inspection."""
        current = self.current_file()
        if current is None:
            return
        try:
            subprocess.Popen(["open", "-a", "Preview", str(current)])
        except OSError as exc:
            self.flash(f"⚠ Could not open Preview: {exc}")
            return
        self.flash(f"Opened {current.name} in Preview (⌘-Tab to come back)")

    # -- zoom / pan ---------------------------------------------------------

    def zoom_to_fit(self):
        self.zoom = None
        self.center = (0.5, 0.5)
        self.render()

    def zoom_to_actual(self):
        self.set_zoom(1.0)

    def zoom_by(self, factor, anchor=None):
        base = self._geom["zoom"] if self._geom else 1.0
        self.set_zoom(base * factor, anchor)

    def toggle_zoom(self, event=None):
        """Double-click / double-tap: fit ↔ 100%, like Preview.app."""
        if self.zoom is None:
            self.set_zoom(1.0, (event.x, event.y) if event else None)
        else:
            self.zoom_to_fit()

    def set_zoom(self, zoom, anchor=None):
        geom = self._geom
        if geom is None:
            return
        old = geom["zoom"]
        new = clamp(zoom, max(MIN_ZOOM, geom["fit_zoom"] * 0.5), MAX_ZOOM)
        if abs(new - old) < 1e-6:
            return
        ow, oh = geom["ow"], geom["oh"]
        if anchor is not None:
            # Keep the image point under the pointer pinned in place.
            ax, ay = anchor
            ix = geom["left"] + (ax - geom["ox"]) / old
            iy = geom["top"] + (ay - geom["oy"]) / old
            vis_w = min(ow, geom["cw"] / new)
            vis_h = min(oh, geom["ch"] / new)
            left = ix - (ax - geom["ox"]) / new
            top = iy - (ay - geom["oy"]) / new
            self.center = ((left + vis_w / 2) / ow, (top + vis_h / 2) / oh)
        self.zoom = None if new <= geom["fit_zoom"] else new
        self.render()

    def on_wheel(self, event, delta=None):
        if delta is None:
            delta = event.delta
        if not delta:
            return
        self.zoom_by(ZOOM_STEP if delta > 0 else 1 / ZOOM_STEP,
                     (event.x, event.y))

    def on_press(self, event):
        self._drag_origin = (event.x, event.y)

    def on_drag(self, event):
        if self._drag_origin is None or self._geom is None:
            return
        dx = event.x - self._drag_origin[0]
        dy = event.y - self._drag_origin[1]
        self._drag_origin = (event.x, event.y)
        geom = self._geom
        ow, oh, zoom = geom["ow"], geom["oh"], geom["zoom"]
        cx = self.center[0] * ow - dx / zoom
        cy = self.center[1] * oh - dy / zoom
        self.center = (cx / ow, cy / oh)
        self.render()

    def on_release(self, event):
        self._drag_origin = None

    # -- rendering ----------------------------------------------------------

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

    def schedule_full_load(self, delay=FULL_SETTLE_MS):
        """Load the current image at full resolution once browsing settles.

        Sharpens the fit view too: one LANCZOS step from the original beats
        downscaling a preview that was already downscaled.
        """
        if self._full_job is not None:
            self.root.after_cancel(self._full_job)
        self._full_job = self.root.after(delay, self._load_full)

    def _load_full(self):
        self._full_job = None
        current = self.current_file()
        if current is None:
            return
        self.loader.drop_full_except([current])
        self.loader.request(current, FULL_TIER)

    def poll_results(self):
        try:
            while True:
                key, decoded, error = self.loader.results.get_nowait()
                self.loader.store(key, decoded if decoded is not None
                                  else (error or "decode failed"))
                current = self.current_file()
                if current is not None and key[0] == str(current):
                    self.render()
        except queue.Empty:
            pass
        self.root.after(25, self.poll_results)

    def _source_for(self, path):
        """Best pixels we have for `path`, plus any error to report."""
        error = None
        for tier in (FULL_TIER, BROWSE_TIER):
            entry = self.loader.get_cached(path, tier)
            if isinstance(entry, Decoded):
                return entry, None
            if isinstance(entry, str) and error is None:
                error = entry
        return None, error

    def render(self):
        canvas = self.canvas
        canvas.delete("all")
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        current = self.current_file()
        self._detail_note = ""

        if current is None:
            canvas.create_text(
                cw // 2, ch // 2, fill="#888888", font=("Helvetica", 20),
                text=f"No images in this view ({FILTER_LABELS[self.filter_mode]})."
                     "\nPress 1 to see all images.",
                justify="center")
            self.update_status()
            return

        source, error = self._source_for(current)
        if source is None and error is not None:
            canvas.create_text(cw // 2, ch // 2, fill="#cc6666",
                               font=("Helvetica", 16), width=max(cw - 80, 200),
                               text=f"Could not load {current.name}\n\n{error}",
                               justify="center")
        elif source is None:
            canvas.create_text(cw // 2, ch // 2, fill="#888888",
                               font=("Helvetica", 18),
                               text=f"Loading {current.name} …")
        elif cw > 2 and ch > 2:
            self._draw_image(source, cw, ch)

        if self.store.is_favorite(rel_name(current, self.directory)):
            canvas.create_text(28, 30, text="★", fill="#ffcc33",
                               font=("Helvetica", 34), anchor="w")

        if self.show_help:
            canvas.create_rectangle(cw // 2 - 300, ch // 2 - 210,
                                    cw // 2 + 300, ch // 2 + 210,
                                    fill="#000000", outline="#555555")
            canvas.create_text(cw // 2, ch // 2, text=HELP_TEXT,
                               fill="#eeeeee", font=("Menlo", 13),
                               justify="left")
        self.update_status()

    def _draw_image(self, source, cw, ch):
        """Draw the visible region of `source`, honouring zoom and pan."""
        ow, oh = source.orig_size
        sw, sh = source.image.size
        src_scale = sw / ow                      # source px per original px
        fit_zoom = min(cw / ow, ch / oh, 1.0)    # original px -> canvas points
        zoom = fit_zoom if self.zoom is None else self.zoom

        # Region of the original image that fits on screen at this zoom.
        vis_w = min(ow, cw / zoom)
        vis_h = min(oh, ch / zoom)
        cx = ow / 2 if vis_w >= ow else clamp(self.center[0] * ow,
                                             vis_w / 2, ow - vis_w / 2)
        cy = oh / 2 if vis_h >= oh else clamp(self.center[1] * oh,
                                             vis_h / 2, oh - vis_h / 2)
        self.center = (cx / ow, cy / oh)         # write back, so pans clamp
        left, top = cx - vis_w / 2, cy - vis_h / 2

        out_w = max(1, min(cw, round(vis_w * zoom)))
        out_h = max(1, min(ch, round(vis_h * zoom)))
        # PIL requires the box to sit inside the source, with a positive area.
        x0 = clamp(left * src_scale, 0, max(0.0, sw - 1e-3))
        y0 = clamp(top * src_scale, 0, max(0.0, sh - 1e-3))
        x1 = clamp((left + vis_w) * src_scale, x0 + 1e-3, sw)
        y1 = clamp((top + vis_h) * src_scale, y0 + 1e-3, sh)
        box = (x0, y0, x1, y1)

        ratio = out_w / max(1e-9, box[2] - box[0])   # output px per source px
        if ratio < 1.0:
            resample = Image.Resampling.LANCZOS     # downscale: keep it sharp
        elif ratio < 2.0:
            resample = Image.Resampling.BICUBIC
        else:
            resample = Image.Resampling.NEAREST     # show the actual pixels
        rendered = source.image.resize((out_w, out_h), resample, box=box)

        # Feed Tk a PPM directly instead of using PIL.ImageTk, which breaks
        # when Pillow and Tk were built against different Tcl/Tk.
        buf = io.BytesIO()
        rendered.save(buf, format="PPM")
        self._photo = self.tk.PhotoImage(data=buf.getvalue())
        ox, oy = (cw - out_w) / 2, (ch - out_h) / 2
        self.canvas.create_image(ox, oy, image=self._photo, anchor="nw")

        self._geom = {"ow": ow, "oh": oh, "cw": cw, "ch": ch, "zoom": zoom,
                      "fit_zoom": fit_zoom, "left": left, "top": top,
                      "ox": ox, "oy": oy}
        self.canvas.config(cursor="fleur" if zoom > fit_zoom else "")

        if source.tier != FULL_TIER and max(source.image.size) < max(ow, oh):
            self._detail_note = ("preview — loading full res…" if zoom > src_scale
                                 else "preview")
        else:
            self._detail_note = "full res"

    def update_status(self):
        current = self.current_file()
        nfav = len(self.store.favorites)
        view_label = FILTER_LABELS[self.filter_mode]
        if current is None:
            text = f"View: {view_label}  —  0 images  —  ★ {nfav} favorites"
        else:
            star = "★" if self.store.is_favorite(
                rel_name(current, self.directory)) else "☆"
            parts = [f"{current.name}",
                     f"{self.index + 1} / {len(self.view)}", star]
            if self._geom:
                ow, oh = self._geom["ow"], self._geom["oh"]
                pct = self._geom["zoom"] * 100
                label = f"{pct:.0f}%" + (" (fit)" if self.zoom is None else "")
                parts += [f"{ow}×{oh}", label]
            if getattr(self, "_detail_note", ""):
                parts.append(self._detail_note)
            parts += [f"View: {view_label}", f"★ {nfav} favorites",
                      "(h for help)"]
            text = "   ".join(parts)
        if self._flash_text:
            text = f"{self._flash_text}   |   {text}"
        if self.save_warning:
            text = f"{self.save_warning}   |   {text}"
        self.status.config(text=text,
                           fg="#ff8844" if self.save_warning else "#dddddd")

    def flash(self, message):
        # Kept as state (not a one-off config call) so a render() in the same
        # event callback can't overwrite it before Tk paints; cleared on a timer.
        if self._flash_job is not None:
            self.root.after_cancel(self._flash_job)
        self._flash_text = message
        self._flash_job = self.root.after(2000, self._clear_flash)
        self.update_status()

    def _clear_flash(self):
        self._flash_text = None
        self._flash_job = None
        self.update_status()

    def on_resize(self, event):
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(80, self.render)

    def toggle_fullscreen(self):
        full = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not full)

    def toggle_help(self):
        self.show_help = not self.show_help
        self.render()

    def on_escape(self, event=None):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
        else:
            self.quit()

    def quit(self):
        if self.save_warning:
            try:
                self.store.save()
            except OSError as exc:
                print(f"WARNING: favorites could not be saved to "
                      f"{self.store.path}: {exc}", file=sys.stderr)
        self.loader.shutdown()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def probe(path, browse_side=FALLBACK_BROWSE_SIDE):
    """Report what each decode path yields for one file, and how fast."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        sys.exit(f"Not a file: {path}")
    print(f"File:      {path}")
    print(f"Size:      {path.stat().st_size / 1e6:.1f} MB")
    print(f"rawpy:     {'yes, LibRaw ' + '.'.join(map(str, rawpy.libraw_version)) if HAVE_RAWPY else 'not installed'}")

    if sys.platform == "darwin":
        dims = _sips_dimensions(path)
        print(f"macOS says the image is: "
              f"{f'{dims[0]}×{dims[1]}' if dims else 'unknown'} pixels")

    if HAVE_RAWPY and path.suffix.lower() in RAW_EXTS:
        with rawpy.imread(str(path)) as raw:
            s = raw.sizes
            print(f"LibRaw:    sensor {s.raw_width}×{s.raw_height}, "
                  f"image {s.iwidth}×{s.iheight}, flip {s.flip}")
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    prev = Image.open(io.BytesIO(thumb.data))
                    print(f"embedded preview: JPEG {prev.width}×{prev.height}"
                          f"  ({len(thumb.data) / 1e6:.1f} MB)")
                else:
                    print(f"embedded preview: bitmap {thumb.data.shape}")
            except Exception as exc:
                print(f"embedded preview: none ({exc})")

    for label, max_side in (("browse tier", browse_side), ("full tier", 0)):
        start = time.perf_counter()
        try:
            decoded = decode_image(path, max_side)
        except Exception as exc:
            print(f"{label:<12} FAILED: {exc}")
            continue
        elapsed = time.perf_counter() - start
        megapixels = decoded.image.width * decoded.image.height / 1e6
        print(f"{label:<12} {decoded.image.width}×{decoded.image.height} "
              f"({megapixels:.1f} MP) in {elapsed:.2f}s   "
              f"original {decoded.orig_size[0]}×{decoded.orig_size[1]}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Fast keyboard-driven culling viewer for CR3 raw images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT)
    parser.add_argument("directory", nargs="?",
                        help="directory containing the images")
    parser.add_argument("--native", action="store_true",
                        help="use the native macOS backend: pixel-exact on "
                             "Retina, pinch/scroll zoom (needs "
                             "pyobjc-framework-Quartz)")
    parser.add_argument("--filter", choices=("all", "fav", "unfav"),
                        default="all", help="initial view filter")
    parser.add_argument("--recursive", action="store_true",
                        help="include images in subdirectories")
    parser.add_argument("--list-fav", action="store_true",
                        help="print favorited filenames and exit (no GUI)")
    parser.add_argument("--list-unfav", action="store_true",
                        help="print non-favorited filenames and exit (no GUI)")
    parser.add_argument("--max-side", type=int, default=None, metavar="PX",
                        help="longest edge of the browse-tier preview; 0 means "
                             "always decode full resolution (default: sized "
                             "to your screen)")
    parser.add_argument("--probe", metavar="FILE",
                        help="report what each decode path yields for FILE "
                             "and exit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.probe:
        return probe(args.probe,
                     FALLBACK_BROWSE_SIDE if args.max_side is None
                     else args.max_side)

    if not args.directory:
        sys.exit("Give me a directory of images (or --probe FILE). "
                 "See --help.")

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        sys.exit(f"Not a directory: {directory}")

    files = scan_images(directory, recursive=args.recursive)
    store = FavoritesStore(directory)

    if args.list_fav or args.list_unfav:
        mode = "fav" if args.list_fav else "unfav"
        for path in apply_filter(files, store.favorites, mode, directory):
            print(rel_name(path, directory))
        return 0

    if not files:
        sys.exit(f"No images found in {directory} "
                 f"(looked for: {', '.join(sorted(ALL_EXTS))})")

    if args.native:
        import native_viewer
        return native_viewer.run(directory, files, store,
                                 initial_filter=args.filter)

    if not HAVE_RAWPY and any(p.suffix.lower() in RAW_EXTS for p in files):
        if sys.platform == "darwin":
            print("Note: rawpy is not installed; using macOS 'sips' to decode "
                  "raw files (slower).\nFor much faster browsing: "
                  "python3 -m pip install rawpy", file=sys.stderr)
        else:
            sys.exit("Raw files found but rawpy is not installed:\n"
                     "    python3 -m pip install rawpy")

    import tkinter as tk
    root = tk.Tk()
    if args.max_side is None:
        # A preview only needs to cover the screen; tkinter draws one image
        # pixel per point, so points are the ceiling. The margin leaves room
        # for a little zooming before the full-resolution copy arrives.
        screen_max = max(root.winfo_screenwidth(), root.winfo_screenheight())
        browse_side = max(2048, int(screen_max * 1.3))
    else:
        browse_side = args.max_side
    loader = ImageLoader(browse_side=browse_side)
    ViewerApp(root, directory, files, store, loader,
              initial_filter=args.filter)
    root.lift()
    root.focus_force()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
