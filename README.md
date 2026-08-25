# PhotoViewer

A fast, keyboard-driven culling tool for Canon **CR3** raw images (JPEG, PNG,
and TIFF work too). Point it at a folder of thousands of shots, flip through
them with the arrow keys, zoom in to check focus, mark favorites, and export
the lists.

## Setup (on the Mac with the photos)

```bash
python3 -m pip install pillow rawpy pyobjc-framework-Quartz
```

On a Retina Mac, install `pyobjc-framework-Quartz` — it is what makes the viewer
sharp, and it becomes the default backend once present. The others are optional:

- `pyobjc-framework-Quartz` enables `--native` (and `--quicklook`). Without it
  you get the tkinter viewer, which **cannot address Retina pixels** — see
  "If it still looks pixelated" below.
- `pillow` is required for the tkinter backend; `--native` does not need it for
  decoding.
- `rawpy` gives fast CR3 decoding by reading the JPEG preview embedded in each
  raw file. `--native` decodes CR3 through macOS itself and doesn't need rawpy
  at all. Without either, the tkinter viewer falls back to macOS's `sips`.

If Python complains that `tkinter` is missing (Homebrew Python), run
`brew install python-tk`.

## Usage

```bash
python3 photo_viewer.py /path/to/your/cr3/folder
```

On macOS that picks the **native** backend automatically when PyObjC is
installed, because it is the only one that can use every pixel of a Retina
display. Override with `--native`, `--quicklook`, or `--tk`.

| Key | Action |
| --- | --- |
| `→` / `↓` | next image |
| `←` / `↑` | previous image |
| `Space` or `F` | toggle ★ favorite |
| `1` | view all images |
| `2` | view favorites only (press again to refresh) |
| `3` | view non-favorites only (press again to refresh) |
| `0` | zoom to fit |
| `9` | zoom to 100% — real pixels |
| `+` / `-` | zoom in / out |
| scroll wheel | zoom at the pointer (`--native`: hold ⌘ or ⌥) |
| drag | pan when zoomed in (`--native`: two-finger scroll) |
| pinch | zoom (`--native` only) |
| double-click | toggle fit ↔ 100% |
| `P` | smooth zoom (like Preview) ↔ hard pixels |
| `Home` / `End` | first / last image |
| `O` | open the current image in Preview.app |
| `E` | export `favorites.txt` and `non_favorites.txt` |
| `G` | toggle fullscreen |
| `H` or `?` | help overlay |
| `Q` / `Esc` | quit |

Zoom level and position **stay put as you arrow between images**, so you can
park on someone's eye at 100% and step through a burst comparing the same spot.

Favorites are saved to `photoviewer_favorites.json` **inside the image
folder** immediately after every change, so you can quit any time and pick up
where you left off. If a save ever fails (locked SD card, full disk) the status
bar turns orange and says so rather than losing your work silently.

## Seeing full resolution

Two resolution tiers are loaded per image:

- a **screen-sized preview**, so holding down the arrow key stays instant, and
- the **full-resolution image**, fetched in the background 350 ms after you
  settle on a shot.

So browsing never waits on a 30 MP decode, but zooming shows genuine pixels
instead of an upscaled preview. The status bar tells you which one you are
looking at (`preview` / `full res`) alongside the true pixel dimensions and the
current zoom percentage. Rendering crops to the visible region *before*
resizing, so zooming into a 45 MP frame never costs more than a screenful of
work.

### If it still looks pixelated

`full res` in the status bar describes the **data**, not the rendering. Three
separate things can still make a full-resolution image look blocky:

1. **You are on the tkinter backend.** Tk draws one image pixel per *point*, so
   on a 2× Retina display "100%" is really 200% on the panel — every image pixel
   becomes a hard 2×2 block. Nothing in the drawing code can fix this; use
   `--native` (now the default when PyObjC is installed).
2. **Hard-pixel mode is on.** Press `P` to go back to smooth. Smooth is the
   default and matches Preview.app; hard pixels are for judging focus.
3. **On a raw file, "full res" may be the JPEG your camera embedded.** Canon
   writes a full-size JPEG into every CR3 and we use it because it is fast and
   already sharpened — but it carries JPEG compression blocks that show up
   under heavy zoom. `--raw-develop` develops the sensor data instead. Run
   `--probe FILE.CR3` to see which one you are getting and what it costs.

### Why `--native` looks sharper

tkinter draws one image pixel per **point**, not per pixel. On a 2× Retina
display that means a fit-to-window photo can only ever use half of the panel's
real pixel grid, no matter how much resolution the file has. This is a hard
limit of Tk, not something the code can tune away.

The `--native` backend sidesteps it by using the same two macOS frameworks
Preview.app uses:

- **ImageIO / CGImageSource** for decoding — Apple's own RAW pipeline, which
  handles `com.canon.cr3-raw-image` directly (so `--native` doesn't even need
  `rawpy`, and colours match Preview).
- **NSScrollView magnification** for zoom — so pinch-to-zoom, double-tap smart
  zoom, and momentum two-finger panning are the system's real implementations.

Because the image is laid out in points (pixels ÷ backing scale factor), 100%
zoom puts exactly one image pixel on one device pixel. That is as sharp as the
display can physically be.

### Piggybacking on Apple's viewer

You cannot drive Preview.app itself — nothing reports which image it is showing
and there is no way to hook its keys, so favoriting would have nothing to attach
to. But you *can* embed Apple's renderer:

```bash
python3 photo_viewer.py /photos --quicklook
```

`--quicklook` puts `QLPreviewView` — the same view behind Finder's spacebar
preview — inside our window, so the image area is entirely Apple's code while
the keys, favorites, filters, and status bar stay ours. Its zoom and scroll
belong to Quick Look, and we hand its `displayState` across navigation so your
zoom usually carries over.

It is not the default because Quick Look does its own loading: there is no
two-tier prefetch, so holding the arrow key down is less instant than
`--native`. Use it to compare Apple's rendering against ours on your own files.

Either way, `O` hands the current file straight to Preview.app if you want a
second opinion.

## Narrowing down your favorites

The intended workflow for culling thousands of shots:

1. First pass: `python3 photo_viewer.py /photos` — arrow through everything,
   press `Space` on keepers. Press `9` to check focus at 100% when unsure.
2. Second pass: press `2` (or launch with `--filter fav`) to see only your
   favorites, and press `Space` again to demote the weaker ones. A demoted
   image stays visible so you can change your mind; press `2` again to refresh
   the view and drop the demoted ones.
3. Repeat until the favorites view is your final selection, then press `E` to
   write `favorites.txt` / `non_favorites.txt`.

Get the lists on the command line (for scripting/copying):

```bash
python3 photo_viewer.py /photos --list-fav     # print favorited filenames
python3 photo_viewer.py /photos --list-unfav   # print the rest
```

Copy your favorites somewhere, for example:

```bash
mkdir -p /photos/keepers
python3 photo_viewer.py /photos --list-fav | while read f; do
  cp "/photos/$f" /photos/keepers/
done
```

## Other options

```
--native                   pixel-exact macOS backend with pinch zoom (default
                           on macOS when PyObjC is installed)
--quicklook                render with Apple's Quick Look view
--tk                       force the portable tkinter backend
--raw-develop              develop raw sensor data for the full tier instead of
                           reusing the camera's embedded JPEG (slower, no
                           compression artifacts)
--filter {all,fav,unfav}   initial view filter
--recursive                include images in subfolders
--max-side N               longest edge of the browse preview (default: sized
                           to your screen); 0 always decodes full resolution
--probe FILE               report what each decode path yields for one file,
                           with timings, and exit
```

`--probe` is the thing to run first on a real CR3 if anything looks off — it
shows the embedded preview size, what LibRaw and macOS each think the image
is, and how long both tiers take to decode:

```bash
python3 photo_viewer.py --probe /photos/IMG_0001.CR3
```

## Development

```bash
python3 -m unittest discover -s tests -q   # logic + geometry, no GUI
python3 tests/smoke_gui.py                 # drives the real tkinter window
python3 tests/smoke_native.py              # drives the real AppKit window
python3 tests/smoke_quicklook.py           # drives the real Quick Look view
```

The smoke tests open a window briefly. The two macOS ones skip themselves if
PyObjC isn't installed. `smoke_gui.py` runs each of its cases in a separate
process, because a second Tk root in one process never gets its geometry
processed on macOS while withdrawn.
