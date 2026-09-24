# Merlin Browser
![version](https://img.shields.io/badge/version-1.6.9-6f8ff0)

### ![warning](https://github.com/DansDesigns/AlternixOS/blob/main/warning.png) V1.6 Requires a reinstall as there has been a fundamental change to the folder structure. ![warning](https://github.com/DansDesigns/AlternixOS/blob/main/warning.png)

A Rust-free desktop web browser built with Python and C++, on Qt and the Chromium engine.


![Merlin](https://github.com/DansDesigns/merlin-browser/blob/main/Screenshot.png?raw=true)


Merlin is a browser shell: the interface, tab handling, content blocking, media
handling and privacy policy are its own, while page rendering is done by
Chromium through Qt WebEngine.


## Features

**Browsing**

- Horizontal tabs, or a vertical strip on either edge that stays narrow and
  widens when you point at it
- Pin a tab to hold it at the start of the strip
- Two-finger swipe to go back and forward, with an arrow that fills as you swipe
- Session restore, with tabs beyond the first loading only when selected
- Find in page, zoom, private windows, downloads, bookmarks and history

**Content blocking**

- Built-in ad and tracker blocker using the Adblock Plus / EasyList filter syntax
- EasyList, EasyPrivacy and Fanboy's Annoyance list, refreshed in the background
- Element hiding through cosmetic filters
- HTTPS upgrade, third-party cookie blocking, `DNT` and `Sec-GPC` headers, and a
  WebRTC local-IP-leak switch
- Per-site controls behind the shield button, with a live blocked count and
  a list of what was blocked on the page
- GPU fingerprinting protection: one generic WebGL vendor and renderer for
  every user, emptied WebGPU adapter descriptors, and a WebGL extension list
  that varies per site so a hash of it cannot follow you around

**Interface**

- Light and dark themes, with toolbar icons drawn to match
- Configurable new tab page: background gradients or your own image, and up to
  five shortcut tiles
- Adjustable page corner rounding, drawn antialiased by an overlay
- A clock in the status bar, and a theme that can follow the time of day
- Optional frameless window, toggled from the menu or with Ctrl+Shift+D
- Twelve search engines with keyword prefixes, plus a custom engine slot
- Right-click selected text to search for it
- Speak to search from the new tab page, recognised on your own machine

**Media**

- Plays H.264 and AAC in the page, YouTube live included, through a codec
  engine the installer and updater put in place on their own
- Plays whatever else the engine supports, and hands the rest to Merlin's own
  player, mpv or VLC
- Three playback modes: player embedded in a tab, player in its own window, or
  in-process libVLC
- `--codecs` reports what your build can decode

**Bringing things across**

- Import bookmarks and history from Chrome, Edge, Brave, Vivaldi, Opera,
  Chromium and Firefox
- Import saved logins from a CSV exported by another browser, kept encrypted by
  DPAPI on Windows or the system keyring elsewhere

**Web apps**

- Install any page as a standalone app with its own shortcut, icon and frameless
  window. Links to other sites open in an ordinary Merlin window
- Settings, Web apps lists the ones you have installed, and removes them

## Requirements

- Python 3.9 or newer
- PyQt6 and PyQt6-WebEngine
- Optional: mpv or VLC for media the engine cannot decode. Merlin's own player
  covers most of it with nothing installed

### YouTube live and other H.264 streams

Ordinary YouTube videos play in any Qt WebEngine because YouTube serves them as
VP9. Most live streams are only offered in H.264 with AAC audio, and the engine
in the PyQt6 wheels is built without those: H.264 and AAC are patented, so Qt
and Chromium leave them out by default.

Brave plays them because it is Chromium built with the codecs compiled in, and
Merlin does the same. Nothing is asked of the user:

- **New installs**: after installing PyQt6, the installer fetches a build of
  the same Qt WebEngine version with H.264 and AAC, published on this
  repository as a release tagged `engine-<version>`. It checks the download
  against its published SHA-256 before unpacking it, swaps it in, starts it,
  and keeps it only if it really plays H.264 and AAC; otherwise the original
  goes straight back. A failure here never stops an install.
- **Existing installs**: after starting, Merlin checks whether its engine
  plays H.264. If not, it fetches the published build in the background and
  puts it in at the next start, before the engine loads, with the same checks
  and rollback.
- **Linux with the distribution's engine**: distribution builds already have
  the codecs, so there is nothing to fetch.

About Merlin shows "licensed codecs on" once the engine has them.

#### Where the builds come from

GitHub builds them. `.github/workflows/build-webengine-codecs.yml` runs on
GitHub's own Windows machines, so nobody compiles Chromium on their computer.
It builds exactly the Qt and Qt WebEngine versions pip installs for Merlin,
checks the result plays H.264 and AAC, and only then publishes it. It checks
once a week and builds only when PyQt6 has moved to a version with nothing
published yet; it can also be started from the Actions tab. The steps that make
Qt WebEngine build on GitHub's runners follow
[danxdigitalsolution-stack/web-engine](https://github.com/danxdigitalsolution-stack/web-engine),
whose public workflow found them first.

Publishing binaries with H.264 and AAC in them carries patent licensing
obligations, which is why Qt does not do it. That is a decision for whoever
publishes the build.

#### Until a build is published

If nothing is published yet for the engine version installed, the standard
engine is kept, and a live stream it cannot play is played by Merlin's own
player instead, by itself, laid over the page's own video at the same size so
the rest of the page stays as it was. Qt's multimedia module ships its own
FFmpeg, separate from the web engine's, and that one decodes H.264.

The stream's address comes from YouTube's own player first. The page, running
in Merlin, has already done everything YouTube asks of a client, so for a live
stream its player holds an HLS address that plays as it is. Only if the page
has none is yt-dlp asked, with Deno to solve YouTube's challenge; Merlin fetches
both from GitHub on first use (yt-dlp about 3 MB, Deno about 40 MB). YouTube
works hard against yt-dlp, so when every way of asking fails, Merlin moves its
own copy to yt-dlp's nightly build, where YouTube fixes usually land first, and
tries once more. Ctrl+Shift+P plays any page in the player the same way.

# Installation

Download from the [Releases](https://github.com/DansDesigns/merlin-browser/releases) page or build from source by cloning this repo then running:

```bash
python3 install-gui.py      # Linux, needs python3-tk
install-gui.bat             # Windows
```

# Install from Terminal (non-GUI):

### Linux

```bash
./install.sh
```

The installer builds a virtualenv, so nothing is added to your system Python.
Two modes are available:

| Mode | Engine | H.264 and AAC |
|---|---|---|
| `--system-qt` | your distribution's Qt WebEngine | yes |
| `--venv-only` | PyQt6 from pip | yes, once a codec build is published |

`--system-qt` is the default when a system PyQt6 is present, because
distribution builds enable the licensed codecs. Add `--yes` to skip the prompts.

Requires `python3-venv`. To install the engine from your distribution first:

```bash
sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine   # Debian, Ubuntu
sudo dnf install python3-qt6 python3-qt6-webengine         # Fedora
sudo pacman -S python-pyqt6 python-pyqt6-webengine         # Arch
```

Uninstall with `~/.local/lib/merlin-browser/uninstall-gui.py` for the graphical
one, or `uninstall.sh` beside it for the text one.

### Windows

```
install.bat
```

Per-user, no administrator rights. It builds a virtualenv in
`%LOCALAPPDATA%\Programs\Merlin`, installs PyQt6 into it, builds `Merlin.exe`
with Merlin's icon, and adds a Start Menu entry.

pip wheels do not include H.264 or AAC; the installer fetches the codec engine
described above to add them. HEVC is not included either way, so for that
install a player:

```
winget install mpv.net
winget install VideoLAN.VLC
```

Uninstall with `%LOCALAPPDATA%\Programs\Merlin\uninstall-gui.bat` for the
graphical one, or `uninstall.bat` beside it for the text one.

### Running without installing

```bash
python3 -m merlin
```

## Command line

```
merlin-browser [URL ...]
  --no-decorations / --decorations   start with or without the title bar
  --persist-decorations              save that choice to settings
  --app URL                          frameless single-purpose window
  --app-name NAME                    window class for it, so the desktop
                                     shows it under its own icon
  --zoom FACTOR                      page zoom for that window
  --private                          off-the-record window
  --profile NAME                     separate storage profile
  --codecs                           print codec and player support
  --timings                          print start-up phase timings
  --icon-check                       report how the application icon resolves
  --embed-icon EXE                   write the icon into a Windows executable
  --version
```

## Keyboard

| | | | |
|---|---|---|---|
| `Ctrl+T` new tab | `Ctrl+W` close tab | `Ctrl+Shift+T` reopen | `Ctrl+Tab` next tab |
| `Ctrl+L` address bar | `Ctrl+R` reload | `Ctrl+Shift+R` hard reload | `Alt+Left/Right` back, forward |
| `Ctrl+F` find | `Ctrl+D` bookmark | `Ctrl+H` history | `Ctrl+U` view source |
| `Ctrl+ +/-/0` zoom | `F11` full screen | `Ctrl+Shift+D` decorations | `Ctrl+,` settings |
| `Ctrl+N` window | `Ctrl+Shift+N` private | `Ctrl+Shift+P` play in player | `Ctrl+Shift+I` pin tab |
| `Ctrl+Shift+L` dark mode | `Ctrl+1..8` tab by index | `Esc` dismiss | |

## Configuration

| | Linux | Windows |
|---|---|---|
| Settings, bookmarks | `~/.config/merlin` | `%APPDATA%\Merlin` |
| History, filter lists | `~/.local/share/merlin` | `%LOCALAPPDATA%\Merlin\data` |
| Cache | `~/.cache/merlin` | `%LOCALAPPDATA%\Merlin\cache` |
| Log, `merlin-log.txt` | `~/.local/state/merlin` | `Documents\Merlin` |
| yt-dlp and Deno | `~/.local/share/merlin/tools` | `%LOCALAPPDATA%\Merlin\tools` |
| Codec engine updates | `~/.local/share/merlin/engine` | `%LOCALAPPDATA%\Merlin\engine` |

Settings are a single `settings.json`; every option in the dialog is a key in
that file.

## Project layout

```
merlin/
  app.py         entry point, engine flags, profile setup, command line
  brand.py       name, version and icon lookup
  browser.py     main window, tabs, toolbar, navigation, downloads
  tabs.py        horizontal and vertical tab strips
  adblock.py     filter parser, matcher and request interceptor
  media.py       codec probing and media player policy
  playertab.py   the media player tab
  inplace.py     Merlin's player laid over a page's own video
  webapps.py     installing pages as standalone apps
  gestures.py    two-finger swipe navigation
  swipeui.py     the swipe progress arrow
  icons.py       toolbar icons, drawn to match the theme
  ui.py          theme, start page, settings dialog
  store.py       history and bookmarks
  settings.py    configuration
  winicon.py     Windows taskbar icon
  winexe.py      writing the icon into a Windows executable
  corners.py     the antialiased rounded corner overlay
  fingerprint.py WebGL and WebGPU de-identification
  passwords.py   saved logins, encrypted by the operating system
  importer.py    reading other browsers' bookmarks and history
  importui.py    the import dialog
  dictation.py   local speech to text for the search box
  single.py      one window, so links reuse the browser already open
  updater.py     version check and in-place update
  codecengine.py fetching, checking and swapping in the codec engine
  crashlog.py    merlin-log.txt, and the stack if Merlin is ever killed
  stdlib_anchor.py  standard modules Merlin.exe carries for future updates
tests/           start-up path checks, and behaviour tests that run the
                 real window against the real engine
tools/           the logo generator, the installer build script, the codec
                 engine tool the installers use, and the stdlib anchor generator
.github/workflows/
                 builds and publishes the codec engine on GitHub
changelog.txt    what changed in each release
```

## Built on

- **[Qt](https://www.qt.io/)** and **[Qt WebEngine](https://doc.qt.io/qt-6/qtwebengine-index.html)** — the widget toolkit and the browser engine, LGPLv3
- **[Chromium](https://www.chromium.org/)** — the rendering engine inside Qt WebEngine, BSD
- **[PyQt6](https://www.riverbankcomputing.com/software/pyqt/)** — Python bindings for Qt, GPLv3 or commercial
- **[EasyList](https://easylist.to/)** — filter lists for the content blocker, CC BY-SA 3.0 / GPLv3
- **[mpv](https://mpv.io/)**, **[VLC](https://www.videolan.org/)** and **[FFmpeg](https://ffmpeg.org/)** — optional media playback
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — fetched on first use, finds the stream on a page for the player
- **[Deno](https://deno.com/)** — fetched on first use, the JavaScript runtime yt-dlp needs for YouTube
- **[GitHub Actions](https://github.com/features/actions)** — builds the Qt WebEngine with H.264 and AAC that the installers fetch

The filter syntax follows the format established by
[Adblock Plus](https://adblockplus.org/filter-cheatsheet) and extended by
[uBlock Origin](https://github.com/gorhill/uBlock/wiki/Static-filter-syntax).

## Known limitations

- HEVC needs a custom engine build; the external player handles it instead
- Scriptlet injections (`##+js(...)`) and the `$removeparam`, `$csp` and
  `$redirect` filter options are parsed and skipped
- Session restore saves URLs, not per-tab history
- Embedded player mode needs X11 or XWayland; window mode covers Wayland
- Reading another browser's saved passwords directly is not supported, and will
  not be: that store is encrypted by the operating system, and opening it means
  shipping the same technique a credential stealer uses. Export to CSV instead

## Updates

Settings, Updates checks `version.txt` in the project repository and reports
whether a newer version exists, with the notes for that release taken from
`changelog.txt`. If one is found, the button becomes "Download and install",
which replaces the application files in place and leaves the virtualenv,
settings, bookmarks and history alone.

`version.txt` holds the version number and nothing else. Everything that shows
a version reads it from there, so there is one place to change it.

Updating does not need the installer. `Merlin.exe` contains a complete copy of
the application, but prefers the ordinary `.py` files in the `app` folder
beside it when they are present. An update replaces those files and takes
effect the next time Merlin starts; if they are missing or damaged the built-in
copy is used, so a failed update cannot stop Merlin running. The executable
only needs rebuilding when a dependency changes, which the changelog will say.

The codec engine arrives the same way, with no reinstall: it is fetched while
Merlin runs and put in place at the next start. On Windows it waits for a start
when no other Merlin window, a web app for instance, has the engine open.

## Licence

Merlin's own code is available under the GPLv3, which is required by its use of
PyQt6. Qt, Qt WebEngine and the filter lists carry their own licences, listed
above.
