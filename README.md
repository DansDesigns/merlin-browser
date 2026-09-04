# Merlin Browser

A desktop web browser built with Python and C++, on Qt and the Chromium engine.


![Merlin](https://github.com/DansDesigns/merlin-browser/blob/main/Screenshot.png?raw=true)


Merlin is a browser shell: the interface, tab handling, content blocking, media
handling and privacy policy are its own, while page rendering is done by
Chromium through Qt WebEngine.

![version](https://img.shields.io/badge/version-1.5.2-6f8ff0)

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
- Per-site controls behind the shield button, with a live blocked count

**Interface**

- Light and dark themes, with toolbar icons drawn to match
- Configurable new tab page: background gradients or your own image, and up to
  five shortcut tiles
- Adjustable page corner rounding
- Optional frameless window, switchable at runtime or from a `.desktop` action
- Twelve search engines with keyword prefixes, plus a custom engine slot
- Right-click selected text to search for it

**Media**

- Plays whatever the engine supports, and hands the rest to mpv or VLC
- Three playback modes: player embedded in a tab, player in its own window, or
  in-process libVLC
- `--codecs` reports what your build can decode

**Web apps**

- Install any page as a standalone app with its own shortcut, icon and frameless
  window

## Requirements

- Python 3.9 or newer
- PyQt6 and PyQt6-WebEngine
- Optional: mpv or VLC for media the engine cannot decode, and yt-dlp for
  streaming sites

## Installation

There is a graphical installer as well. It runs the same scripts and shows the
same steps, with the detail available behind a toggle rather than filling the
screen:

```bash
python3 install-gui.py      # Linux, needs python3-tk
install-gui.bat             # Windows
```

To hand someone a single file with nothing to extract, build the installer into
one executable. Run this on Windows; a Windows executable has to be built on
Windows:

```
tools\build-installer.bat
```

That produces `dist\MerlinSetup.exe`, the graphical installer with the merlin
package and `install.bat` packed inside it. It is the same method used for
Merlin.exe itself: PyInstaller, with the icon in the executable's resources.

### Linux

```bash
./install.sh
```

The installer builds a virtualenv, so nothing is added to your system Python.
Two modes are available:

| Mode | Engine | H.264 and AAC |
|---|---|---|
| `--system-qt` | your distribution's Qt WebEngine | yes |
| `--venv-only` | PyQt6 from pip | no |

`--system-qt` is the default when a system PyQt6 is present, because
distribution builds enable the licensed codecs. Add `--yes` to skip the prompts.

Requires `python3-venv`. To install the engine from your distribution first:

```bash
sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine   # Debian, Ubuntu
sudo dnf install python3-qt6 python3-qt6-webengine         # Fedora
sudo pacman -S python-pyqt6 python-pyqt6-webengine         # Arch
```

Uninstall with `~/.local/lib/merlin-browser/uninstall.sh`.

### Windows

```
install.bat
```

Per-user, no administrator rights. It builds a virtualenv in
`%LOCALAPPDATA%\Programs\Merlin`, installs PyQt6 into it, creates `Merlin.exe`
with Merlin's icon, and adds Start Menu entries for the normal and frameless
launch.

pip wheels do not include H.264, AAC or HEVC, and there is no distribution
package on Windows, so install a player for those formats:

```
winget install mpv.net
winget install VideoLAN.VLC
```

Uninstall with `%LOCALAPPDATA%\Programs\Merlin\uninstall.bat`.

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
  webapps.py     installing pages as standalone apps
  gestures.py    two-finger swipe navigation
  swipeui.py     the swipe progress arrow
  icons.py       toolbar icons, drawn to match the theme
  ui.py          theme, start page, settings dialog
  store.py       history and bookmarks
  settings.py    configuration
  winicon.py     Windows taskbar icon
  winexe.py      writing the icon into a Windows executable
tests/           checks for the platform-specific start-up paths
tools/           the logo generator
```

## Built on

- **[Qt](https://www.qt.io/)** and **[Qt WebEngine](https://doc.qt.io/qt-6/qtwebengine-index.html)** — the widget toolkit and the browser engine, LGPLv3
- **[Chromium](https://www.chromium.org/)** — the rendering engine inside Qt WebEngine, BSD
- **[PyQt6](https://www.riverbankcomputing.com/software/pyqt/)** — Python bindings for Qt, GPLv3 or commercial
- **[EasyList](https://easylist.to/)** — filter lists for the content blocker, CC BY-SA 3.0 / GPLv3
- **[mpv](https://mpv.io/)**, **[VLC](https://www.videolan.org/)** and **[FFmpeg](https://ffmpeg.org/)** — optional media playback
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — optional, resolves streaming sites for the player

The filter syntax follows the format established by
[Adblock Plus](https://adblockplus.org/filter-cheatsheet) and extended by
[uBlock Origin](https://github.com/gorhill/uBlock/wiki/Static-filter-syntax).

## Known limitations

- HEVC needs a custom engine build; the external player handles it instead
- Scriptlet injections (`##+js(...)`) and the `$removeparam`, `$csp` and
  `$redirect` filter options are parsed and skipped
- Session restore saves URLs, not per-tab history
- Embedded player mode needs X11 or XWayland; window mode covers Wayland
- Corner rounding uses a region mask, which is not antialiased, so a large
  radius may show slight stair-stepping

## Updates

Settings, Updates checks `version.txt` in the project repository and reports
whether a newer version exists. It never downloads or installs anything.

## Licence

Merlin's own code is available under the GPLv3, which is required by its use of
PyQt6. Qt, Qt WebEngine and the filter lists carry their own licences, listed
above.
