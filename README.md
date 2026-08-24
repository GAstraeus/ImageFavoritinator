# PhotoViewer

A fast, keyboard-driven culling tool for Canon **CR3** raw images (JPEG, PNG,
and TIFF work too). Point it at a folder of thousands of shots, flip through
them with the arrow keys, zoom in to check focus, mark favorites, and export
the lists.

## Setup (on the Mac with the photos)

```bash
python3 -m pip install pillow rawpy pyobjc-framework-Quartz
```

Only `pillow` is strictly required:

- `rawpy` gives fast CR3 decoding by reading the JPEG preview embedded in each
  raw file. Without it the viewer falls back to macOS's built-in `sips`, which
  works with nothing installed but is slower per image.
- `pyobjc-framework-Quartz` enables `--native`, which is the sharpest option on
  a Retina display (see below).

If Python complains that `tkinter` is missing (Homebrew Python), run
`brew install python-tk`.

## Usage

```bash
python3 photo_viewer.py /path/to/your/cr3/folder            # tkinter viewer
python3 photo_viewer.py /path/to/your/cr3/folder --native    # sharpest on Retina
```

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
work, and past 2× it switches to nearest-neighbour — if you are pixel-peeping,
you should see the actual pixels rather than a smoothed guess.

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
--native                   pixel-exact macOS backend with pinch zoom
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
```

The two smoke tests open a window briefly. `smoke_native.py` skips itself if
PyObjC isn't installed. `smoke_gui.py` runs each of its cases in a separate
process, because a second Tk root in one process never gets its geometry
processed on macOS while withdrawn.
