#!/usr/bin/env python3
"""
photo_viewer.py — fast keyboard-driven culling tool for Canon CR3 raw images
(also works with JPEG / PNG / TIFF).

Usage:
    python3 photo_viewer.py /path/to/images

Keys:
    Right / Down        next image
    Left / Up           previous image
    Space or F          toggle favorite on the current image
    1                   view all images
    2                   view favorites only
    3                   view non-favorites only
    Home / End          jump to first / last image in the current view
    E                   export favorites.txt and non_favorites.txt
    G                   toggle fullscreen
    H or ?              toggle help overlay
    Q / Escape          quit (Escape leaves fullscreen first)

Favorites are saved to photoviewer_favorites.json in the image directory
after every change, so it is always safe to quit.

CR3 decoding uses the embedded JPEG preview via rawpy (pip install rawpy)
when available, which is very fast. Without rawpy it falls back to macOS's
built-in `sips` tool, which is slower but needs nothing installed.
"""

import argparse
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
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

# Decoded previews are downscaled so their longest side is at most this many
# pixels before caching; big enough for any laptop/monitor, small enough that
# a dozen cached previews stay well under a few hundred MB.
DEFAULT_MAX_SIDE = 2560
CACHE_SIZE = 12
PREFETCH_AHEAD = 3
PREFETCH_BEHIND = 1
# Workers skip decodes for images no longer within this many positions of the
# current one (stale requests from holding down an arrow key).
RELEVANCE_WINDOW = 8


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


def _decode_raw_rawpy(path):
    with rawpy.imread(str(path)) as raw:
        flip = getattr(raw.sizes, "flip", 0)
        thumb = None
        try:
            thumb = raw.extract_thumb()
        except Exception:
            pass  # fall through to full decode

        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            img = Image.open(io.BytesIO(thumb.data))
            img.load()
            orientation = img.getexif().get(0x0112, 1)
            if orientation and orientation != 1:
                img = ImageOps.exif_transpose(img)
            elif flip:
                img = _apply_libraw_flip(img, flip)
            return img
        if thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
            return _apply_libraw_flip(Image.fromarray(thumb.data), flip)

        # No usable embedded preview: do a fast half-size demosaic.
        rgb = raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
        return Image.fromarray(rgb)  # postprocess already applies orientation


def _decode_raw_sips(path, max_side):
    """Decode a raw file with macOS's built-in sips (no pip installs needed)."""
    fd, out = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-Z", str(max_side),
             str(path), "--out", out],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"sips failed: {result.stderr.strip() or result.stdout.strip()}")
        img = Image.open(out)
        img.load()
        return img
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def decode_image(path, max_side=DEFAULT_MAX_SIDE):
    """Decode any supported image to an RGB PIL image, longest side <= max_side."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in RAW_EXTS:
        if HAVE_RAWPY:
            img = _decode_raw_rawpy(path)
        elif sys.platform == "darwin":
            img = _decode_raw_sips(path, max_side)
        else:
            raise RuntimeError(
                "Raw decoding needs rawpy on this platform: "
                "python3 -m pip install rawpy")
    else:
        img = Image.open(path)
        img.load()
        img = ImageOps.exif_transpose(img)
    if img.width > max_side or img.height > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img.convert("RGB")


class ImageLoader:
    """Decodes images on worker threads with an LRU cache.

    tkinter is not thread-safe, so workers never touch the GUI: results land
    on a queue.Queue that the main thread drains from a `root.after` timer.
    """

    def __init__(self, max_side=DEFAULT_MAX_SIDE, cache_size=CACHE_SIZE,
                 workers=2, is_relevant=None):
        self.max_side = max_side
        self.cache_size = cache_size
        self.is_relevant = is_relevant or (lambda path: True)
        self.results = queue.Queue()
        self._cache = OrderedDict()   # str(path) -> PIL.Image
        self._pending = set()
        self._lock = threading.Lock()
        # Daemon threads (not ThreadPoolExecutor): executor workers are
        # non-daemon and get joined at interpreter exit, so quitting would
        # block for seconds on an in-flight sips/rawpy decode.
        self._tasks = queue.Queue()
        self._workers = []
        for _ in range(workers):
            thread = threading.Thread(target=self._worker_loop, daemon=True)
            thread.start()
            self._workers.append(thread)

    def get_cached(self, path):
        key = str(path)
        with self._lock:
            img = self._cache.get(key)
            if img is not None:
                self._cache.move_to_end(key)
            return img

    def request(self, path):
        """Ask for a decode if not already cached or in flight."""
        key = str(path)
        with self._lock:
            if key in self._cache or key in self._pending:
                return
            self._pending.add(key)
        self._tasks.put(path)

    def _worker_loop(self):
        while True:
            path = self._tasks.get()
            if path is None:
                return
            self._work(path)

    def _work(self, path):
        key = str(path)
        try:
            if not self.is_relevant(path):
                return  # stale request; skip the expensive decode
            try:
                img = decode_image(path, self.max_side)
                self.results.put((key, img, None))
            except Exception as exc:
                self.results.put((key, None, str(exc)))
        finally:
            with self._lock:
                self._pending.discard(key)

    def store(self, key, img):
        """Called by the main thread when draining the results queue."""
        with self._lock:
            self._cache[key] = img
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def shutdown(self):
        for _ in self._workers:
            self._tasks.put(None)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

FILTER_LABELS = {"all": "All", "fav": "Favorites", "unfav": "Non-favorites"}

HELP_TEXT = """\
Right / Down      next image
Left / Up         previous image
Space or F        toggle favorite
1                 view all
2                 view favorites only
3                 view non-favorites only
                  (re-press 2/3 to refresh after toggling)
Home / End        first / last image
E                 export favorites.txt / non_favorites.txt
G                 toggle fullscreen
H or ?            toggle this help
Q / Escape        quit"""


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
        self._photo = None          # keep a reference or tk drops the image
        self._resize_job = None
        self._flash_text = None
        self._flash_job = None
        self.save_warning = None    # persistent banner when saving fails

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
        root.bind("q", lambda e: self.quit())
        root.bind("Q", lambda e: self.quit())
        root.bind("<Escape>", self.on_escape)
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

    def _is_relevant(self, path):
        """Workers call this (from their thread) to skip stale decodes."""
        view, idx = self.view, self.index
        try:
            return abs(view.index(Path(path)) - idx) <= RELEVANCE_WINDOW
        except ValueError:
            return False

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

    # -- rendering ----------------------------------------------------------

    def show_current(self):
        current = self.current_file()
        if current is not None:
            self.loader.request(current)
            self.prefetch()
        self.render()

    def prefetch(self):
        ahead = PREFETCH_AHEAD if self.direction > 0 else PREFETCH_BEHIND
        behind = PREFETCH_BEHIND if self.direction > 0 else PREFETCH_AHEAD
        for offset in list(range(1, ahead + 1)) + \
                      [-o for o in range(1, behind + 1)]:
            i = self.index + offset
            if 0 <= i < len(self.view):
                self.loader.request(self.view[i])

    def poll_results(self):
        try:
            while True:
                key, img, error = self.loader.results.get_nowait()
                if img is not None:
                    self.loader.store(key, img)
                else:
                    self.loader.store(key, error or "decode failed")
                current = self.current_file()
                if current is not None and key == str(current):
                    self.render()
        except queue.Empty:
            pass
        self.root.after(25, self.poll_results)

    def render(self):
        canvas = self.canvas
        canvas.delete("all")
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        current = self.current_file()

        if current is None:
            canvas.create_text(
                cw // 2, ch // 2, fill="#888888", font=("Helvetica", 20),
                text=f"No images in this view ({FILTER_LABELS[self.filter_mode]})."
                     "\nPress 1 to see all images.",
                justify="center")
            self.update_status()
            return

        cached = self.loader.get_cached(current)
        if cached is None:
            canvas.create_text(cw // 2, ch // 2, fill="#888888",
                               font=("Helvetica", 18),
                               text=f"Loading {current.name} …")
        elif isinstance(cached, str):
            canvas.create_text(cw // 2, ch // 2, fill="#cc6666",
                               font=("Helvetica", 16), width=max(cw - 80, 200),
                               text=f"Could not load {current.name}\n\n{cached}",
                               justify="center")
        elif cw > 2 and ch > 2:
            scale = min(cw / cached.width, ch / cached.height, 1.0)
            size = (max(1, int(cached.width * scale)),
                    max(1, int(cached.height * scale)))
            resized = cached.resize(size, Image.Resampling.BILINEAR)
            # Feed Tk a PPM directly instead of using PIL.ImageTk, which
            # breaks when Pillow and Tk were built against different Tcl/Tk.
            buf = io.BytesIO()
            resized.save(buf, format="PPM")
            self._photo = self.tk.PhotoImage(data=buf.getvalue())
            canvas.create_image(cw // 2, ch // 2, image=self._photo)

        if self.store.is_favorite(rel_name(current, self.directory)):
            canvas.create_text(28, 30, text="★", fill="#ffcc33",
                               font=("Helvetica", 34), anchor="w")

        if self.show_help:
            canvas.create_rectangle(cw // 2 - 260, ch // 2 - 170,
                                    cw // 2 + 260, ch // 2 + 170,
                                    fill="#000000", outline="#555555")
            canvas.create_text(cw // 2, ch // 2, text=HELP_TEXT,
                               fill="#eeeeee", font=("Menlo", 14),
                               justify="left")
        self.update_status()

    def update_status(self):
        current = self.current_file()
        nfav = len(self.store.favorites)
        view_label = FILTER_LABELS[self.filter_mode]
        if current is None:
            text = f"View: {view_label}  —  0 images  —  ★ {nfav} favorites"
        else:
            star = "★" if self.store.is_favorite(
                rel_name(current, self.directory)) else "☆"
            text = (f"{current.name}   {self.index + 1} / {len(self.view)}"
                    f"   {star}   View: {view_label}"
                    f"   ★ {nfav} favorites   (h for help)")
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
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Fast keyboard-driven culling viewer for CR3 raw images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT)
    parser.add_argument("directory", help="directory containing the images")
    parser.add_argument("--filter", choices=("all", "fav", "unfav"),
                        default="all", help="initial view filter")
    parser.add_argument("--recursive", action="store_true",
                        help="include images in subdirectories")
    parser.add_argument("--list-fav", action="store_true",
                        help="print favorited filenames and exit (no GUI)")
    parser.add_argument("--list-unfav", action="store_true",
                        help="print non-favorited filenames and exit (no GUI)")
    parser.add_argument("--max-side", type=int, default=DEFAULT_MAX_SIDE,
                        help="longest preview edge in pixels "
                             f"(default {DEFAULT_MAX_SIDE})")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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
    loader = ImageLoader(max_side=args.max_side)
    ViewerApp(root, directory, files, store, loader,
              initial_filter=args.filter)
    root.lift()
    root.focus_force()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
