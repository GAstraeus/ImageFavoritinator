# PhotoViewer

A fast, keyboard-driven culling tool for Canon **CR3** raw images (JPEG, PNG,
and TIFF work too). Point it at a folder of thousands of shots, flip through
them with the arrow keys, mark favorites, and export the lists.

## Setup (on the Mac with the photos)

```bash
python3 -m pip install pillow rawpy
```

`rawpy` gives fast CR3 decoding by reading the JPEG preview embedded in each
raw file. If you skip it, the viewer still works on macOS using the built-in
`sips` tool — it's just slower per image.

If Python complains that `tkinter` is missing (Homebrew Python), run
`brew install python-tk`.

## Usage

```bash
python3 photo_viewer.py /path/to/your/cr3/folder
```

| Key | Action |
| --- | --- |
| `→` / `↓` | next image |
| `←` / `↑` | previous image |
| `Space` or `F` | toggle ★ favorite |
| `1` | view all images |
| `2` | view favorites only |
| `3` | view non-favorites only |
| `Home` / `End` | first / last image |
| `E` | export `favorites.txt` and `non_favorites.txt` |
| `G` | toggle fullscreen |
| `H` or `?` | help overlay |
| `Q` / `Esc` | quit |

Favorites are saved to `photoviewer_favorites.json` **inside the image
folder** immediately after every change, so you can quit any time and pick up
where you left off.

## Narrowing down your favorites

The intended workflow for culling thousands of shots:

1. First pass: `python3 photo_viewer.py /photos` — arrow through everything,
   press `Space` on keepers.
2. Second pass: press `2` (or launch with `--filter fav`) to see only your
   favorites, and press `Space` again to demote the weaker ones. A demoted
   image stays visible (so you can change your mind); press `2` again to
   refresh the view and drop the demoted ones.
3. Repeat until the favorites view is your final selection, then press `E` to
   write `favorites.txt` / `non_favorites.txt`.

Get the lists on the command line (for scripting/copying):

```bash
python3 photo_viewer.py /photos --list-fav     # print favorited filenames
python3 photo_viewer.py /photos --list-unfav   # print the rest
```

Copy your favorites somewhere, for example:

```bash
python3 photo_viewer.py /photos --list-fav | while read f; do
  cp "/photos/$f" /photos/keepers/
done
```

## Other options

```
--filter {all,fav,unfav}   initial view filter
--recursive                include images in subfolders
--max-side N               preview resolution (default 2560 px longest edge)
```

## Development

Run the unit tests (no GUI needed):

```bash
python3 -m unittest discover -s tests -v
```
