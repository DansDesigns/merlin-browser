"""Media playback for codecs the engine will not touch.

Chromium decodes `<video>` internally. There is no supported way to substitute
libVLC for Chromium's decoders without forking Chromium's media stack, so
"embed VLC" cannot mean "make `<video>` use VLC". What it can mean is running a
full player next to the page, and there are three ways to do that with very
different consequences. Measured on Ubuntu, 2026-08-26:

  embedded  Separate player process, its video surface reparented into a Merlin
            tab via mpv's --wid or VLC's --drawable-xid. Looks embedded while
            the decoder stays in its own address space, so a codec crash takes
            the player down and not the browser.  <- default

  window    Same, minus the reparenting: the player opens its own window. The
            most robust option, and the only one that works on native Wayland.

  libvlc    True in-process libVLC through python-vlc, rendering into a QWidget.
            Tighter integration, at the cost of running the whole FFmpeg stack
            inside the browser process.

Codec coverage in every mode, verified by decoding real files: H.264 yes,
H.265/HEVC yes, AAC yes, plus everything else FFmpeg supports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

CODEC_PROBE_JS = r"""
(function () {
  var v = document.createElement('video');
  var a = document.createElement('audio');
  var tests = [
    ['H.264 / AVC',   'video', 'video/mp4; codecs="avc1.42E01E"'],
    ['H.265 / HEVC',  'video', 'video/mp4; codecs="hvc1.1.6.L93.B0"'],
    ['AV1',           'video', 'video/mp4; codecs="av01.0.05M.08"'],
    ['VP9',           'video', 'video/webm; codecs="vp9"'],
    ['VP8',           'video', 'video/webm; codecs="vp8"'],
    ['Theora',        'video', 'video/ogg; codecs="theora"'],
    ['AAC',           'audio', 'audio/mp4; codecs="mp4a.40.2"'],
    ['MP3',           'audio', 'audio/mpeg'],
    ['Opus',          'audio', 'audio/webm; codecs="opus"'],
    ['Vorbis',        'audio', 'audio/webm; codecs="vorbis"'],
    ['FLAC',          'audio', 'audio/flac'],
    ['WAV / PCM',     'audio', 'audio/wav']
  ];
  var out = [];
  for (var i = 0; i < tests.length; i++) {
    var el = tests[i][1] === 'video' ? v : a;
    out.push([tests[i][0], el.canPlayType(tests[i][2]) || 'no']);
  }
  return JSON.stringify({
    codecs: out,
    ua: navigator.userAgent,
    drm: typeof navigator.requestMediaKeySystemAccess === 'function'
  });
})()
"""

# Watches for media elements the engine refuses to decode, so the browser can
# offer the fallback instead of leaving the user staring at a dead player.
MEDIA_ERROR_WATCH_JS = r"""
(function () {
  if (window.__merlinMediaWatch) return;
  window.__merlinMediaWatch = true;
  function srcOf(el) {
    var src = el.currentSrc || el.src || '';
    if (!src) {
      var s = el.querySelector('source[src]');
      if (s) src = s.src;
    }
    return src;
  }
  function hook(el) {
    if (el.__merlinHooked) return;
    el.__merlinHooked = true;
    el.addEventListener('error', function () {
      var e = el.error;
      if (e && (e.code === 3 || e.code === 4)) {
        var s = srcOf(el);
        if (s) window.__merlinFailedMedia = s;
      }
    }, true);
  }
  function scan() {
    var list = document.querySelectorAll('video, audio');
    for (var i = 0; i < list.length; i++) hook(list[i]);
  }
  scan();
  new MutationObserver(scan).observe(document.documentElement,
                                     {childList: true, subtree: true});
})();
"""

FAILED_MEDIA_JS = "window.__merlinFailedMedia || ''"

PATENT_ENCUMBERED = {
    "H.264 / AVC": "MPEG LA AVC pool",
    "H.265 / HEVC": "HEVC Advance / Access Advance pools",
    "AAC": "Via Licensing AAC pool",
}

PLAYER_CANDIDATES = ("mpv", "vlc", "cvlc", "mplayer", "celluloid")

# Windows players are usually not on PATH, so look where the installers put them.
WINDOWS_PLAYER_PATHS = (
    r"%ProgramFiles%\VideoLAN\VLC\vlc.exe",
    r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe",
    r"%ProgramFiles%\mpv\mpv.exe",
    r"%LOCALAPPDATA%\Programs\mpv\mpv.exe",
    r"%ProgramFiles%\mpv.net\mpvnet.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\mpv.exe",
)

MEDIA_EXTENSIONS = (
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts",
    ".m2ts", ".mpg", ".mpeg", ".ogv", ".m3u8", ".mpd",
    ".mp3", ".flac", ".aac", ".m4a", ".opus", ".ogg", ".wav", ".wma", ".dts",
)

LIBVLC_NOTE = (
    "In-process libVLC loads the whole FFmpeg stack into the browser, including "
    "libraries Merlin never calls. The two process-separated modes give "
    "identical codec coverage while keeping the decoder in its own process, so "
    "a crash there cannot take your tabs with it."
)


# --------------------------------------------------------------- discovery
def find_player(preferred: str = "") -> str:
    if preferred:
        path = preferred if os.path.isabs(preferred) else shutil.which(preferred)
        if path and os.path.exists(path):
            return path
    for name in PLAYER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    if os.name == "nt":
        for template in WINDOWS_PLAYER_PATHS:
            path = os.path.expandvars(template)
            if "%" not in path and os.path.exists(path):
                return path
    return ""


def has_ytdlp() -> bool:
    return bool(shutil.which("yt-dlp") or shutil.which("youtube-dl"))


def has_libvlc() -> bool:
    try:
        import vlc  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return False
    return True


def libvlc_version() -> str:
    try:
        import vlc

        return vlc.libvlc_get_version().decode("utf-8", "replace")
    except Exception:                                    # noqa: BLE001
        return ""


def embedding_supported() -> tuple[bool, str]:
    """Reparenting a player's video output needs a native window handle."""
    if os.name == "nt":
        return True, ""                      # HWND embedding
    if sys.platform == "darwin":
        return False, ("macOS has no equivalent of --wid for these players; "
                       "use separate-window mode")
    if sys.platform != "linux":
        return False, "Window embedding is implemented for X11 and Windows only."
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return False, ("native Wayland session: player windows cannot be "
                       "reparented. Run under XWayland (QT_QPA_PLATFORM=xcb) or "
                       "switch to separate-window mode")
    return True, ""


def looks_like_media(url: str) -> bool:
    path = url.split("?")[0].split("#")[0].lower()
    return path.endswith(MEDIA_EXTENSIONS)


# ----------------------------------------------------------------- commands
def player_command(player: str, url: str, window_id: int = 0,
                   extra: str = "") -> list[str]:
    """Build argv, embedding into window_id when one is given.

    mpv exposes --wid; VLC exposes --drawable-xid, confirmed present in this
    VLC build by reading the xcb_window plugin's option table.
    """
    base = os.path.basename(player).lower()
    base = base[:-4] if base.endswith(".exe") else base
    argv = [player]

    if base in ("mpv", "mpvnet"):
        argv += ["--force-window=immediate", "--keep-open=no",
                 "--osc=yes", "--input-default-bindings=yes"]
        if window_id:
            # mpv takes --wid on both X11 (XID) and Windows (HWND)
            argv.append(f"--wid={int(window_id)}")
    elif base in ("vlc", "cvlc"):
        argv += ["--play-and-exit", "--no-video-title-show"]
        if window_id:
            # VLC names the option per windowing system
            flag = "--drawable-hwnd" if os.name == "nt" else "--drawable-xid"
            argv.append(f"{flag}={int(window_id)}")
    elif base == "mplayer":
        if window_id:
            argv += ["-wid", str(int(window_id))]
    if extra:
        argv += extra.split()
    argv.append(url)
    return argv


def launch(url: str, player: str = "", window_id: int = 0,
           extra: str = "") -> tuple[bool, str, object]:
    player = find_player(player)
    if not player:
        return False, ("No player found. Install mpv or VLC to play the formats "
                       "the engine does not ship with."), None
    if not url:
        return False, "No media URL was available for that element.", None
    try:
        proc = subprocess.Popen(
            player_command(player, url, window_id, extra),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=not window_id,
        )
    except OSError as exc:
        return False, f"Could not start {os.path.basename(player)}: {exc}", None
    where = "in this tab" if window_id else "in its own window"
    return True, f"Playing with {os.path.basename(player)} {where}", proc


# ------------------------------------------------------------------ report
def parse_probe(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def summarise(probe: dict) -> str:
    codecs = dict(probe.get("codecs", []))
    missing = [name for name in PATENT_ENCUMBERED if codecs.get(name, "no") == "no"]
    if not missing:
        return "All common codecs available in the engine, licensed ones included."
    return ("Engine is missing: " + ", ".join(missing) +
            ". Patent-encumbered, so most open-source builds omit them. The "
            "player fallback covers all of them.")


def codec_report_html(probe: dict, player: str, ytdlp: bool, mode: str,
                      libvlc: str = "") -> str:
    rows = []
    for name, verdict in probe.get("codecs", []):
        if verdict in ("probably", "maybe"):
            colour, label = "#4ec97a", verdict
        else:
            colour, label = "#e2585d", "not supported"
        note = ""
        if verdict == "no" and name in PATENT_ENCUMBERED:
            note = (f"<span class=n>{PATENT_ENCUMBERED[name]} &middot; "
                    f"covered by the player</span>")
        rows.append(f"<tr><td>{name}</td><td style='color:{colour}'>{label}</td>"
                    f"<td>{note}</td></tr>")

    mode_text = {
        "embedded": "Player runs as a separate process, reparented into a tab.",
        "window": "Player opens its own window.",
        "libvlc": "In-process libVLC, sharing this browser's process.",
        "off": "Fallback playback is disabled.",
    }.get(mode, mode)

    player_line = (f"Player: <b>{os.path.basename(player)}</b>" if player
                   else "Player: <b>none found</b>")
    libvlc_line = f" &middot; libVLC <b>{libvlc}</b>" if libvlc else ""

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Codec support</title><style>
body{{font:14px/1.6 system-ui,sans-serif;background:#17181c;color:#e6e6ea;
      margin:0;padding:32px;}}
h1{{font-size:22px;margin:0 0 6px;}} h2{{font-size:15px;margin:26px 0 8px;color:#9fb2ee;}}
p.sub{{color:#8d8f98;margin:0 0 20px;}}
table{{border-collapse:collapse;width:100%;max-width:780px;}}
td{{padding:7px 10px;border-bottom:1px solid #26282f;}} td:first-child{{width:170px;}}
.n{{color:#8d8f98;font-size:12px;}}
ul{{max-width:780px;color:#c9cbd3;}} li{{margin:6px 0;}}
code{{background:#22242b;padding:2px 6px;border-radius:4px;font-size:12.5px;}}
.box{{background:#1e2027;border-left:3px solid #6f8ff0;padding:12px 16px;
      max-width:780px;margin:14px 0;border-radius:4px;}}
</style></head><body>
<h1>Codec support</h1>
<p class="sub">Reported by the engine itself through <code>canPlayType()</code>.</p>
<table>{''.join(rows)}</table>
<h2>Fallback player</h2>
<div class="box">{mode_text}</div>
<p>{player_line}{libvlc_line} &middot; yt-dlp:
<b>{'installed' if ytdlp else 'not installed'}</b></p>
<ul>
<li>Right-click a video and choose <b>Play with Merlin's player</b>, or press
<code>Ctrl+Shift+P</code> to send the current page to it.</li>
<li>The player decodes through FFmpeg, which covers H.264, HEVC, AAC and
everything else the engine refuses.</li>
<li>yt-dlp resolves streaming sites to direct media URLs.</li>
</ul>
</body></html>"""


# ------------------------------------------------------------- CLI entry
def main_standalone() -> int:
    """Print codec support and playback capability, then exit."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6 import QtWebEngineWidgets  # noqa: F401
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtCore import QTimer

    app = QApplication([sys.argv[0]])
    view = QWebEngineView()
    view.setHtml("<html><body></body></html>")
    state = {"code": 1}

    def probe():
        def got(raw):
            data = parse_probe(raw)
            codecs = data.get("codecs", [])
            if not codecs:
                print("Could not probe the engine.")
                app.quit()
                return
            width = max(len(name) for name, _ in codecs)
            print("Engine codec support")
            print("=" * (width + 24))
            for name, verdict in codecs:
                mark = "yes" if verdict in ("probably", "maybe") else "NO"
                note = ""
                if mark == "NO" and name in PATENT_ENCUMBERED:
                    note = f"   ({PATENT_ENCUMBERED[name]})"
                print(f"{name:<{width}}  {mark:<3} {verdict:<9}{note}")
            print()
            print(summarise(data))
            print()
            player = find_player()
            print("Fallback player :", os.path.basename(player) if player else "none")
            print("yt-dlp          :", "installed" if has_ytdlp() else "not installed")
            print("libVLC bindings :", libvlc_version() or "not installed")
            ok, why = embedding_supported()
            print("Tab embedding   :", "available" if ok else f"unavailable, {why}")
            state["code"] = 0
            app.quit()

        view.page().runJavaScript(CODEC_PROBE_JS, got)

    QTimer.singleShot(1500, probe)
    QTimer.singleShot(15000, app.quit)
    app.exec()
    return state["code"]
