# Merlin Browser

A privacy-focused desktop browser built with Python and C++, on Qt and the
Chromium engine. It has a built-in ad and tracker blocker, horizontal or vertical
tabs, a window-decoration toggle, two-finger swipe navigation, and a media player
for the formats the engine does not ship with.

---

## The name

Nova was taken, so: **Merlin**, the smallest British falcon, which breeds on
Dartmoor. Same family as Kestrel without the collision.

Checked before choosing, because that is what went wrong last time. Already taken
in the browser namespace: **Falkon** (kills falcon), **BlackHawk** (kills hawk),
plus Otter, Lynx, Puffin, Dolphin, Basilisk, SeaMonkey, Ladybird, Sleipnir and
Firefox. Merlin is clear as a browser; it is busy elsewhere (a 6502 assembler, a
Cornell bird-ID app), which is why the binary is `merlin-browser`.

Still clear if you want a different one: **Shrike**, **Chough**, **Marten**,
**Dipper**.

Branding is confined to `merlin/brand.py`, so the name is not baked into the
code the way it was before:

```bash
./rename.sh shrike     # moves the package, rewrites identifiers and .desktop files
```

---

## Can we embed VLC?

Not in the sense of making a page's `<video>` element decode through it:
Chromium's media pipeline is internal, and substituting libVLC for its decoders
means forking Chromium's media stack.

What Merlin does instead is run a full player alongside the page, three ways:

| Mode | What it is |
|---|---|
| **embedded** (default) | mpv or VLC as a child process, its video surface reparented into a Merlin tab via `--wid` / `--drawable-xid` |
| **window** | same, but the player opens its own window |
| **libvlc** | in-process libVLC via python-vlc, drawing into a Qt widget |

The default keeps the decoder in its own process, so a codec crash takes the
player down rather than your tabs, and the tab still looks like it is playing the
video itself.

Codec coverage, verified by decoding real files rather than reading a feature
list:

```
H.264        fourcc=avc1   decoded=YES
H.265/HEVC   fourcc=hvc1   decoded=YES
AAC          decoded=YES
```

All three are formats the engine itself cannot handle. Right-click any video and
choose **Play video with Merlin's player**, press `Ctrl+Shift+P` for the current
page, or navigate straight to a `.mkv` and Merlin opens the player tab instead of
downloading it. When a page's `<video>` fails with a decode error, Merlin notices
and offers the player rather than leaving you with a dead rectangle.

```bash
sudo apt install mpv                       # or vlc
pip install --user yt-dlp                  # streaming sites
```

Reparenting a player window needs an X11 window id, so **embedded mode requires
X11 or XWayland**. On native Wayland, Merlin says so and falls back to window
mode.


## Codecs in the engine itself

Separate from the player: the engine's own `<video>` support depends on who built
it, and it is a patent question rather than a technical one.

| Codec | pip wheels | distro package |
|---|---|---|
| AV1, VP9, VP8, Opus, Vorbis, MP3, FLAC, WAV | yes | yes |
| **H.264 / AVC** | no | **yes** |
| **AAC** | no | **yes** |
| H.265 / HEVC | no | no |

So install the engine from your distro, not pip:

```bash
sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine   # Debian / Ubuntu
sudo dnf install python3-qt6 python3-qt6-webengine         # Fedora
sudo pacman -S python-pyqt6 python-pyqt6-webengine         # Arch
```

That covers H.264 and AAC in-page. HEVC is the one nobody ships; the player
handles it.

`merlin-browser --codecs` prints exactly what your build supports.

---

## Folder structure

This matters: `merlin/` must be a real subfolder, because Python needs it to be
an importable package. Downloading the files individually, or with a "download
all" button, flattens that and the installer then reports it cannot find
`merlin/app.py`.

```
merlin-browser/
├── install.sh                        Linux installer
├── install.bat                       Windows installer
├── uninstall.bat                     Windows uninstaller
├── setup-layout.py                   repairs a flattened download
├── rename.sh                         change the project name
├── requirements.txt
├── README.md
├── merlin-browser                    launcher script
├── merlin-browser.desktop            desktop entry, with a frameless action
├── merlin-browser-frameless.desktop
├── merlin/merlin.ico                 7-size Windows icon (inside the package)
├── merlin/merlin.png                 256px icon
├── merlin/merlin.svg                 scalable icon
├── merlin-run.py                     what the launchers actually execute
├── version.txt                       template for the repo update check
└── merlin/                           <-- the package; keep this folder
    ├── __init__.py
    ├── __main__.py                   python3 -m merlin
    ├── app.py                        entry point, flags, profile setup
    ├── brand.py                      every string carrying the name
    ├── browser.py                    window, tabs, frameless handling
    ├── adblock.py                    filter engine and interceptor
    ├── media.py                      codec probing and player policy
    ├── playertab.py                  the player tab
    ├── tabs.py                       horizontal and vertical tab strips
    ├── ui.py                         theme, settings dialog, start page
    ├── settings.py                   config handling
    └── store.py                      history and bookmarks
```

**If your download came out flat**, put every file in one folder and run:

```bash
python3 setup-layout.py
```

It moves the twelve modules into `merlin/`, restores executable bits, fixes line
endings (Windows `goto` breaks on LF), recovers files whose extension the browser
stripped, and then compiles each module to prove nothing arrived truncated. Safe
to run more than once. The `merlin-browser.zip` archive avoids the problem
entirely, since a zip carries its own directory structure.

---

## Install

Both installers build an **isolated virtualenv**. Nothing is installed into your
system Python, and neither can break the other. On current Debian and Ubuntu this
is also the only thing that works: `pip --user` now fails with
`externally-managed-environment` under PEP 668.

Both installers keep a progress bar pinned to the bottom of the window while
everything else scrolls above it, using a terminal scroll region, and print each
command as it runs. pip shows its own download progress rather than leaving you
at a blinking cursor for the minutes it takes to fetch 150 MB of engine. Piping
the output to a file gives plain text with no escape sequences.

**Linux**

```bash
./install.sh                # picks the best mode for your machine
./install.sh --system-qt    # reuse the distro engine (licensed codecs)
./install.sh --venv-only    # fully self-contained (pip engine, no H.264)
./install.sh --yes          # no prompts
```

The two modes trade isolation against codecs:

| Mode | venv | Engine | H.264 / AAC |
|---|---|---|---|
| `--system-qt` | `--system-site-packages` | your distro's | **yes** |
| `--venv-only` | sealed | pip wheels | no |

`--system-qt` is the default when a system PyQt6 exists. It does not simply
trust that, though: it reports which engine actually resolved (a `pip --user`
install can shadow the distro one) and then **asks the engine what it can
decode** rather than claiming licensed codecs it may not have.

Installs to `~/.local/lib/merlin-browser`, with `~/.local/lib/merlin-browser/uninstall.sh`
to reverse it.

**Windows**

```
install.bat
```

Per-user, no administrator rights. It finds Python (the `py` launcher or a bare
`python.exe`), builds a venv in `%LOCALAPPDATA%\Programs\Merlin\venv`, installs
PyQt6 into it, writes Start Menu entries for the normal and frameless launch,
and adds itself to PATH. The system Python is used
only to create the venv and is otherwise untouched. `uninstall.bat` removes the
lot and keeps your profile unless you say otherwise. It copies itself to `%TEMP%`
and re-runs from there first, because Windows will not delete a folder that
holds the running batch file, which showed up as a permissions error.

Two launchers are installed: `merlin-browser` for normal use, and
`merlin-console` for `--codecs`, which needs a console to print into. Both point straight at the venv interpreter, so no `PYTHONPATH` is involved.

Windows notes:

* There is no distribution package, so pip is the only route and the engine will
  lack **H.264, AAC and HEVC**. A player is not optional there:
  `winget install mpv.net` or `winget install VideoLAN.VLC`.
* Player embedding uses HWND rather than X11 window ids: mpv still takes
  `--wid`, VLC takes `--drawable-hwnd`, in-process libVLC uses `set_hwnd`.

Run without installing, on either platform: `python3 -m merlin`.

## Updates

Settings, Updates checks `version.txt` in
`DansDesigns/merlin_browser` and compares the first line with the running
version. It never downloads or installs anything: it tells you a newer version
exists, shows the notes and links to the releases page.

Put a `version.txt` in the repository root, `main` or `master` branch, like the
one shipped here:

```
1.2.0
Short description of what changed.
```

The first line is the version, everything after it is release notes. Both
branch names are tried, so it works whichever your repo uses. If the file does
not exist yet the check reports `HTTP 404` rather than failing silently.

## Gestures

Two-finger swipe **left to right goes back**, **right to left goes forward**, on
trackpads and touchscreens alike. A circular arrow grows out of the window edge
as you swipe, with a ring showing how much further you have to go. Reverse or
let go before the ring closes and nothing happens; reach the threshold and it
flashes and navigates.

The travel is deliberately long, 260px on a trackpad, and a swipe must also last
at least 160ms: a quick flick sideways while scrolling should never take you back
a page. If a gesture simply stops part-way, the arrow fades after a second rather
than sitting there, because trackpads do not always send an end-of-gesture
event. Turn it off, or reverse the direction if your trackpad
reports the opposite, in Settings, General.


Two-finger swipe **left to right goes back**, **right to left goes forward**,
on trackpads and touchscreens alike. Turn it off, or reverse the direction if
your trackpad reports the opposite, in Settings, General.

Trackpads and touchscreens arrive as completely different events, so both are
handled: horizontal wheel events with phase tracking for the former, raw
two-point touch for the latter. The filter sits on the application rather than
the web view, because QtWebEngine renders into a private child widget that
consumes input before a view-level filter would see it. Horizontal movement has
to beat vertical by 1.6x before anything happens, so ordinary scrolling with a
bit of sideways drift never navigates, and a 0.6s cooldown stops one long flick
walking several steps through history.

## Start-up speed

Merlin should be on screen in well under a second. If it is not, run
`merlin-console --timings` (or `merlin-browser --timings` on Linux) and you will
get a phase-by-phase breakdown.

What used to make it crawl, and what changed:

* **Filter parsing blocked start-up.** EasyList, EasyPrivacy and Fanboy together
  are around 270,000 rules, and every one had its regex compiled up front:
  roughly 34 seconds before the window appeared. Regexes now compile on first
  use, plain `||domain^` rules skip regex entirely in favour of a hostname
  index, and the whole parse happens on a worker thread that swaps a finished
  engine in when it is ready. Parsing dropped to under two seconds and no longer
  blocks anything. Measured: window visible in 0.3s with 270,000 rules and 20
  restored tabs.
* **Session restore loaded every tab at once.** Twenty tabs meant twenty
  simultaneous page loads. Restored tabs beyond the first are now deferred and
  fetch when you select them.
* **The address bar completer rebuilt after every page load**, re-querying
  history and rebuilding a model over thousands of URLs. It now refreshes at
  most every 30 seconds.
* **Filter lists re-downloaded and re-parsed on every launch.** The refresh now
  runs only when the cache is more than 12 hours old, and only re-parses if
  something actually changed.

## New tab shortcuts

The tiles on the new tab page are yours: up to five, edited in Settings,
Shortcuts, where you can rename, re-address, reorder, remove or reset them. A
**+** tile appears on the page itself while there is room for another, and
clicking it asks for an address and a name.

## Search

Settings, Search lists twelve engines in a table showing each one's name, the
address it actually queries (`google.com/search`, `search.brave.com/search`) and
its keyword prefix: DuckDuckGo, Google, Bing, Brave, Startpage, Ecosia, Qwant,
Mojeek, Yahoo, Wikipedia, YouTube, plus a Custom slot taking any URL with `{}`
where the query goes.

Keyword prefixes let you use another engine once without changing the default:
`wiki merlin`, `yt qt tutorial`, `g some query`. The panel previews the exact URL
your current setting produces.

## Interface too large or too small

Settings, Appearance, Interface size. `System default` follows Windows display
scaling, which on a scaled display can leave everything bigger than you want;
8pt to 12pt override it outright.

## Behaving like a normal Windows application

Merlin installs its own **`Merlin.exe`**: a copy of the virtualenv's
`pythonw.exe` placed beside it in `venv\Scripts`. Same interpreter, same DLL
resolution, but its own process name, its own taskbar identity, and something
Windows can pin. Shortcuts point at it directly with the script as a quoted
argument.

That last part matters for pinning. A shortcut aimed at a `.cmd`, which is what
earlier versions used, cannot be pinned usefully: Windows pins the batch file,
and since it launched the browser through `start` the pinned button had no
process to attach to and did nothing when clicked.

The icon reaches the taskbar three ways, because they cover different states:

* the `.lnk` carries `IconLocation`, which is what a pinned-but-not-running tile
  shows;
* the window carries the icon through Qt;
* and it is pushed onto the window handle with `WM_SETICON`, the message the
  shell reads for a running button, then again once the taskbar button exists.

An explicit **Application User Model ID is off by default**. It makes Windows
resolve the button icon by hunting for a Start Menu shortcut carrying the same
ID, and nothing available to the installer can write that ID onto a `.lnk`, so
the lookup fails and Windows falls back to the executable's icon. With a real
`Merlin.exe` the process already has an identity to group and pin by, so the ID
only reintroduces the failed lookup. `MERLIN_APP_ID=1` enables it anyway.

The `.ico` is also formed the way Windows expects: DIB entries below 128px and
PNG only above. Every entry was PNG once, including 16x16, which several Windows
APIs reject outright.

`merlin-console --icon-check` prints which executable is running, where the icon
came from, whether Windows can read it and whether `WM_SETICON` was accepted.

Windows caches taskbar icons hard: if a pinned entry still shows the old one,
unpin and re-pin it.

## If Merlin will not start on Windows

`pythonw.exe` has no console, so a start-up failure used to produce nothing at
all. That cannot happen now: `merlin-run.py` catches anything thrown during
start-up, writes it to `%LOCALAPPDATA%\Merlin\startup-error.log` and shows it in
a message box.

For a live console, run `merlin-debug.cmd` from the install folder.

Shortcuts take **no arguments at all**: each one points at its own `.cmd`
launcher (`merlin-browser.cmd`, `merlin-frameless.cmd`). Every launch failure so
far has been an argument string mangled between cmd and PowerShell, so there is
now nothing left to mangle. The installer reads each shortcut back and confirms
its target exists before claiming success. The
installer also reads each shortcut back after creating it and prints the exact
command it will run, so a broken shortcut is visible at install time rather than
the first time you click it.

## Tabs

Close buttons sit on the **left** inside each horizontal tab, and a **+** at the
end of the strip opens a new one.

In the vertical strip the close button sits on the **right** instead, so the
favicon keeps a fixed left inset and does not move as the strip expands: the
expansion only reveals what is to the right of it. Measured at 0%, 50% and 100%,
the favicon holds the same x position throughout.

Settings, Appearance, Tab bar position:

* **Across the top** — the usual horizontal strip.
* **Vertical, left edge** / **Vertical, right edge** — a narrow column showing
  favicons only, which widens to full titles when the pointer moves over it and
  narrows again when it leaves.

Tabs for pages Merlin renders itself carry a matching toolbar icon: the home
icon for the new tab page, the bookmark icon for the bookmarks list. Tabs showing
the new tab page carry the home icon, since they have no favicon of
their own and a blank square in a narrow strip tells you nothing. The tab rows
scroll while the **+** stays pinned at the bottom of the collapsed column: with
both in one centred column, a long tab list pushed the button off the bottom of
the screen, and hovering slid it sideways along with the strip.

Only the **collapsed column** is painted as a panel. The width the strip gains on
hover stays transparent, and each row becomes its own rounded chip: titles need
something to sit on to stay readable, but the gaps between the chips keep the
page visible, so hovering never drops a solid slab over what you are reading.
Collapsed, the rows are already on the column, so they stay clear and only the
active one is marked.

The vertical strip **floats above the page** and only reserves its collapsed
width in the layout. Expanding by relayout would reflow the web view on every
hover, which looks bad and costs a full page layout each time. With decorations
hidden, its empty area drags the window, since there is no title bar or
horizontal strip left to grab.

## Appearance

* **Toolbar icons** are drawn from built-in SVG paths and tinted to the theme:
  white on dark, black on light. Desktop theme icons are whatever colour their
  designer chose and vanish against the opposite background, so they are not
  used.
* **New tab background** — eight built-in gradients and flats, or your own
  image. Custom images are inlined into the page as data URIs, capped at 6 MB,
  since the start page is rendered with `setHtml` and has no file access.
* **Bookmarks button** in the toolbar drops down your bookmarks; clicking one
  opens it. `Ctrl+D` still bookmarks the current page.
* **Page corner rounding** — the page area is clipped to rounded corners to
  match the rest of the interface. The web view paints into its own surface and
  ignores a CSS border-radius, so the corners are cut with a region mask built
  from a painter path. Square, Slight, Rounded and Very rounded, in Appearance.
* **Dark mode** toggles from the menu, directly under New private window, or
  with `Ctrl+Shift+L`.
* The toolbar keeps single actions left of the address bar (back, forward,
  reload, home, bookmark this page) and menus to the right (shields, bookmarks,
  main menu).

## Window decorations

| Path | How |
|---|---|
| Settings | Menu, Settings, Appearance |
| Keyboard | `Ctrl+Shift+D`, live, tabs preserved |
| Command line | `--no-decorations` / `--decorations`, `--persist-decorations` to save |
| `.desktop` | The bundled entry has an "Open without window decorations" action |

Frameless: drag the empty tab strip to move, drag any edge or corner to resize
(native `startSystemMove` / `startSystemResize`, so Wayland works), double-click
the tab strip to maximise. Window buttons appear at the right of the toolbar and
can be switched off for tiling WMs. Maximising a frameless window sets the
geometry to the screen's available area rather than calling `showMaximized()`,
which keeps the taskbar visible and avoids the window-manager negotiation over a
frame that does not exist. The close button is permanently red rather
than red-on-hover, because with no title bar it is the only close affordance on
screen. `--app URL` opens a frameless single-purpose window.

---

## Shields

EasyList-syntax engine, the same rule format Brave and uBlock Origin use.
Network rules with `$` options and `@@` exceptions, cosmetic `##` rules,
token-indexed matching, EasyList plus EasyPrivacy plus Fanboy fetched in the
background, HTTPS upgrade, third-party cookie blocking, `DNT` and `Sec-GPC`,
WebRTC leak switch, per-site toggle with a live blocked count.

---

## Keyboard

| | | | |
|---|---|---|---|
| `Ctrl+T` new tab | `Ctrl+W` close tab | `Ctrl+Shift+T` reopen | `Ctrl+Tab` next tab |
| `Ctrl+L` address bar | `Ctrl+R` reload | `Ctrl+Shift+R` hard reload | `Alt+Left/Right` back / forward |
| `Ctrl+F` find | `Ctrl+D` bookmark | `Ctrl+H` history | `Ctrl+U` view source |
| `Ctrl+ +/-/0` zoom | `F11` full screen | `Ctrl+Shift+D` decorations | `Ctrl+,` settings |
| `Ctrl+N` window | `Ctrl+Shift+N` private | `Ctrl+Shift+P` play in player | `Esc` dismiss |
| `Ctrl+Shift+L` dark mode | | | |

Right-click selected text to search for it with your current engine.

---

## Layout

```
merlin/
  brand.py       every string that carries the name; rename starts here
  app.py         entry point, Chromium flags, profile setup, --codecs
  browser.py     window, tabs, toolbar, frameless handling, player routing
  adblock.py     EasyList parser, token-indexed matcher, request interceptor
  media.py       codec probing, player command construction, the mode policy
  playertab.py   the player tab: external process or in-process libVLC
  tabs.py        horizontal and vertical tab strips
  icons.py       theme-tinted toolbar icons
  gestures.py    two-finger swipe navigation
  swipeui.py     the growing back/forward arrow
  updater.py     GitHub version check
  ui.py          theme, start page, tab bar, window buttons, settings dialog
  store.py       sqlite history, JSON bookmarks
  settings.py    config: ~/.config/merlin on Linux, %APPDATA%\Merlin on Windows
```

```
install.sh / install.bat     per-user installers, both venv-based
uninstall.bat                Windows removal, keeps the profile by default
rename.sh                    change the project name in one shot
merlin.ico / merlin.png      7-size Windows icon and a 256px PNG
```

## Command line

```
merlin-browser [URL ...]
  --no-decorations / --decorations   frameless or not, this launch only
  --persist-decorations              also write that choice to settings
  --app URL                          frameless single-purpose window
  --private                          off-the-record window
  --profile NAME                     separate storage profile
  --codecs                           print codec and player capability, exit
  --version                          print the version and exit
  --timings                          print start-up phase timings
  --icon-check                       report how the application icon resolves
```

## What is tested, and what is not

Tested by running it: the filter engine, blocking on a live server, cosmetic
filtering, the decoration toggle, session handling, the codec probe on two
different engine builds, libVLC decoding H.264, HEVC and AAC, both tab layouts
including the hover animation, and the whole browser booting on Qt 6.4 and 6.9.

Also tested by running it: `install.sh` end to end in both modes, including the
venv isolation check (engine resolving from inside the venv, `sys.prefix` split
from `sys.base_prefix`) and the installed launcher.

Not tested, because this environment has no display server and no Windows:
player window reparenting and `install.bat` end to end. Their inputs are checked statically (every `goto`
resolves to a label, the files are CRLF so `goto` parses on Windows, the
generated launcher content is verified) but nobody has double-clicked the thing
on a real desktop yet. Treat those three as first-run-and-report.

## Known limits

* Embedded player mode needs X11 or XWayland; window mode covers native Wayland.
* The reparenting path is untested by me for lack of a display server. Flag
  construction, process handling and the libVLC path are tested.
* HEVC in-page needs a custom engine build; the player covers it.
* Scriptlets (`##+js(...)`) and `$removeparam`, `$csp`, `$redirect` are skipped.
* Session restore saves URLs, not per-tab back/forward history.
