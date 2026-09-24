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


# ------------------------------------------------------------------- yt-dlp
#
# yt-dlp turns a page on a streaming site into the address of the stream
# itself, which a player can then open. It matters most for YouTube live: those
# streams are usually offered only in H.264, which the web engine was built
# without, while Qt's own multimedia module decodes it fine.
#
# One on the PATH is used if present. Otherwise the official build is fetched
# from the project's GitHub releases on first use, after asking, into Merlin's
# data folder, where it can update itself. It is not bundled into Merlin.exe:
# sites change often and yt-dlp is updated to match, so a copy frozen into the
# executable would go stale and need a rebuild to fix.

YTDLP_RELEASES = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"

# Run on a YouTube page: the address of its live stream, from YouTube's own
# player. The page has already done everything YouTube demands of a client,
# challenges included, so its player response carries an HLS address that
# needs nothing more: audio and video together, in H.264, which Merlin's
# built-in player decodes. No yt-dlp involved, so none of the trouble YouTube
# makes for it.
#
# The response must belong to the video on screen. YouTube moves between
# videos without loading a new page, and the one set at first load
# (ytInitialPlayerResponse) goes stale, so the player's current one comes
# first and anything for a different video is ignored.
YOUTUBE_STREAM_JS = r"""
(function () {
  try {
    var want = new URLSearchParams(location.search).get('v') || '';
    if (!want) {
      var live = location.pathname.match(/^\/live\/([\w-]{6,})/);
      if (live) { want = live[1]; }
    }
    var found = [];
    var player = document.getElementById('movie_player');
    if (player && player.getPlayerResponse) {
      try { found.push(player.getPlayerResponse()); } catch (e) {}
    }
    if (window.ytInitialPlayerResponse) { found.push(window.ytInitialPlayerResponse); }
    for (var i = 0; i < found.length; i++) {
      var r = found[i];
      if (!r || !r.streamingData) { continue; }
      var id = r.videoDetails && r.videoDetails.videoId;
      if (want && id && id !== want) { continue; }
      var hls = r.streamingData.hlsManifestUrl || '';
      if (hls) {
        return {hls: hls, title: (r.videoDetails && r.videoDetails.title) || ''};
      }
    }
    return {hls: '', title: ''};
  } catch (e) {
    return {hls: '', title: '', error: String(e)};
  }
})()
"""

# Run on a YouTube page: is this a live stream, and can the engine play H.264?
# Several signals, since YouTube is a single page application and the one set
# on first load is not updated when you move between videos.
YOUTUBE_LIVE_JS = r"""
(function () {
  try {
    var video = document.createElement('video');
    var h264 = video.canPlayType('video/mp4; codecs="avc1.4d401e"');
    var live = false;
    var player = document.getElementById('movie_player');
    if (player && player.getVideoData) {
      var data = player.getVideoData();
      if (data && data.isLive) { live = true; }
    }
    var badge = document.querySelector('.ytp-live-badge');
    if (badge && badge.offsetParent !== null &&
        !badge.hasAttribute('disabled')) { live = true; }
    if (document.querySelector('.ytp-live')) { live = true; }
    var failed = !!document.querySelector('.ytp-error');
    return {live: live, h264: h264, failed: failed};
  } catch (e) {
    return {live: false, h264: 'unknown', failed: false};
  }
})()
"""


def ytdlp_folder() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Merlin", "tools")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(base, "merlin", "tools")


def _ytdlp_asset() -> str:
    return "yt-dlp.exe" if os.name == "nt" else "yt-dlp"


def ytdlp_path() -> str:
    """The yt-dlp to use: the one Merlin fetched, else one on the PATH.

    Merlin's own copy comes first. It is the official build, which carries
    the challenge solver scripts YouTube now needs; a copy installed some
    other way, with pip or a package manager, may not.
    """
    local = os.path.join(ytdlp_folder(), _ytdlp_asset())
    if os.path.isfile(local):
        return local
    return shutil.which("yt-dlp") or ""


# -------------------------------------------------------------------- deno
#
# Since late 2025 YouTube only hands its video formats to a client that can
# solve a JavaScript challenge. yt-dlp does that with an external JavaScript
# runtime, and without one YouTube offers nothing but thumbnails, which yt-dlp
# reports as "Requested format is not available".
#
# Deno is the runtime yt-dlp recommends and enables by default, and it runs
# the challenge scripts with no file system or network access. Fetched from
# Deno's own GitHub releases on first use, like yt-dlp.

DENO_RELEASES = "https://github.com/denoland/deno/releases/latest/download/"


def _machine() -> str:
    """The processor architecture, from the environment rather than platform.

    Not the platform module: Merlin.exe only contains the standard modules
    that were in use when it was built, and platform was not, so importing it
    from an update failed with "No module named 'platform'".
    """
    if os.name == "nt":
        return (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE") or "AMD64").lower()
    try:
        return os.uname().machine.lower()
    except AttributeError:
        return "x86_64"


def _deno_asset() -> str:
    machine = _machine()
    arm = machine in ("aarch64", "arm64")
    if os.name == "nt":
        return "deno-x86_64-pc-windows-msvc.zip"
    if sys.platform == "darwin":
        return ("deno-aarch64-apple-darwin.zip" if arm
                else "deno-x86_64-apple-darwin.zip")
    return ("deno-aarch64-unknown-linux-gnu.zip" if arm
            else "deno-x86_64-unknown-linux-gnu.zip")


def _deno_name() -> str:
    return "deno.exe" if os.name == "nt" else "deno"


def deno_path() -> str:
    """Deno for yt-dlp: the one Merlin fetched, else one on the PATH."""
    local = os.path.join(ytdlp_folder(), _deno_name())
    if os.path.isfile(local):
        return local
    return shutil.which("deno") or ""


def fetch_deno(timeout: int = 300) -> tuple[bool, str]:
    """Download Deno from its GitHub releases into Merlin's tools folder."""
    import tempfile
    import urllib.request
    import zipfile

    folder = ytdlp_folder()
    target = os.path.join(folder, _deno_name())
    try:
        os.makedirs(folder, exist_ok=True)
        request = urllib.request.Request(
            DENO_RELEASES + _deno_asset(),
            headers={"User-Agent": "Merlin Browser"})
        with tempfile.TemporaryDirectory() as scratch:
            archive = os.path.join(scratch, "deno.zip")
            with urllib.request.urlopen(request, timeout=timeout) as response, \
                    open(archive, "wb") as out:
                shutil.copyfileobj(response, out)
            with zipfile.ZipFile(archive) as bundle:
                names = [n for n in bundle.namelist()
                         if os.path.basename(n) == _deno_name()]
                if not names:
                    return False, "The Deno download did not contain deno."
                partial = target + ".part"
                with bundle.open(names[0]) as source, open(partial, "wb") as out:
                    shutil.copyfileobj(source, out)
            os.replace(partial, target)
        if os.name != "nt":
            os.chmod(target, 0o755)
    except Exception as exc:                      # noqa: BLE001
        return False, f"Could not fetch Deno: {exc}"

    ok, version = deno_version()
    if not ok:
        return False, f"Deno was fetched but will not run: {version}"
    return True, f"Deno {version} is ready"


def deno_version() -> tuple[bool, str]:
    path = deno_path()
    if not path:
        return False, "not installed"
    try:
        done = _run_helper([path, "--version"], timeout=30)
    except Exception as exc:                      # noqa: BLE001
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout).strip()[:200]
    first = done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""
    return True, first.replace("deno ", "").split(" ")[0] or first


def _needs_js_runtime(url: str) -> bool:
    host = url.split("/")[2].lower() if url.count("/") >= 2 else ""
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def has_ytdlp() -> bool:
    return bool(ytdlp_path() or shutil.which("youtube-dl"))


_HELPERS: set = set()


def _run_helper(argv: list, timeout: int, record: list = None) -> subprocess.CompletedProcess:
    """Run yt-dlp or Deno, and remember it until it finishes.

    A lookup still running when Merlin closes would otherwise carry on for
    up to its whole timeout, keeping a process alive after the window has
    gone. stop_helpers() ends anything still listed here.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, **_hidden())
    _HELPERS.add(proc)
    if record is not None:
        record.append(proc)          # so a caller can stop it early
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise
    finally:
        _HELPERS.discard(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def stop_helpers() -> int:
    """End any yt-dlp or Deno process still running. Returns how many."""
    stopped = 0
    for proc in list(_HELPERS):
        if proc.poll() is None:
            try:
                proc.kill()
                stopped += 1
            except Exception:                     # noqa: BLE001
                pass
    _HELPERS.clear()
    return stopped


def _hidden() -> dict:
    """No console window flashing up for a helper process on Windows."""
    if os.name != "nt":
        return {}
    return {"creationflags": 0x08000000}          # CREATE_NO_WINDOW


def _ytdlp_argv(path: str) -> list:
    # The Linux release is a Python zip application. Run it with this
    # interpreter rather than trusting a shebang, which may name a python
    # that is not there.
    if os.name != "nt" and not getattr(sys, "frozen", False):
        try:
            with open(path, "rb") as handle:
                if handle.read(2) == b"#!":
                    return [sys.executable, path]
        except OSError:
            pass
    return [path]


def fetch_ytdlp(timeout: int = 120) -> tuple[bool, str]:
    """Download the official yt-dlp build into Merlin's tools folder."""
    import urllib.request

    folder = ytdlp_folder()
    target = os.path.join(folder, _ytdlp_asset())
    partial = target + ".part"
    try:
        os.makedirs(folder, exist_ok=True)
        request = urllib.request.Request(
            YTDLP_RELEASES + _ytdlp_asset(),
            headers={"User-Agent": "Merlin Browser"})
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                open(partial, "wb") as out:
            shutil.copyfileobj(response, out)
        if os.path.getsize(partial) < 100_000:
            os.remove(partial)
            return False, "The download was too small to be yt-dlp."
        os.replace(partial, target)
        if os.name != "nt":
            os.chmod(target, 0o755)
    except Exception as exc:                      # noqa: BLE001
        try:
            os.remove(partial)
        except OSError:
            pass
        return False, f"Could not fetch yt-dlp: {exc}"

    ok, version = ytdlp_version()
    if not ok:
        return False, f"yt-dlp was fetched but will not run: {version}"
    return True, f"yt-dlp {version} is ready"


def ytdlp_version() -> tuple[bool, str]:
    path = ytdlp_path()
    if not path:
        return False, "not installed"
    try:
        done = _run_helper(_ytdlp_argv(path) + ["--version"], timeout=30)
    except Exception as exc:                      # noqa: BLE001
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout).strip()[:200]
    return True, done.stdout.strip()


JS_RUNTIME_MISSING = (
    "YouTube only gives its video to a client that can solve a JavaScript "
    "challenge, and yt-dlp needs a JavaScript runtime, Deno, to do that.")


# How to ask YouTube for something playable, tried in order until one works.
#
# YouTube's ordinary web client now gets only SABR formats, which yt-dlp
# cannot download and skips, and when nothing else is left it reports
# "Requested format is not available". yt-dlp's own PO Token guide gives the
# way round it: the web_safari client provides HLS formats that need no PO
# token, and HLS live streams need none either. An HLS format also carries
# audio and video together, which is exactly what Merlin's player wants.
#
# So HLS from web_safari first, then the TV client, then yt-dlp's own choice.
YOUTUBE_ATTEMPTS = (
    ("HLS through the Safari client",
     ["--extractor-args", "youtube:player_client=web_safari",
      "-f", "best[protocol^=m3u8]/best[acodec!=none][vcodec!=none]/best"]),
    ("the TV client",
     ["--extractor-args", "youtube:player_client=tv",
      "-f", "best[protocol^=m3u8]/best[acodec!=none][vcodec!=none]/best"]),
    ("yt-dlp's default clients",
     ["-f", "best[protocol^=m3u8]/best[acodec!=none][vcodec!=none]/best"]),
)
OTHER_ATTEMPTS = (
    ("yt-dlp's default choice",
     ["-f", "best[acodec!=none][vcodec!=none]/best"]),
)


_UPDATED_THIS_SESSION = False


def _is_youtube(url: str) -> bool:
    return _needs_js_runtime(url)


def _telling_lines(text: str) -> list:
    """The lines of yt-dlp's output that say why, not the whole transcript."""
    keep = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("ERROR", "WARNING")) and line not in keep:
            keep.append(line)
    return keep


# ------------------------------------------------------- YouTube cookies
#
# YouTube refuses yt-dlp far less often when it arrives with the browsing
# session the page already has: "The page needs to be reloaded" is its check
# for a client it does not trust. This keeps Merlin's own YouTube and Google
# cookies, as the engine reports them, so a lookup can present them. Handing
# browser cookies to yt-dlp is the remedy its own --cookies-from-browser
# option provides; only these two sites' cookies are kept, in memory, and the
# file yt-dlp reads them from is deleted as soon as it finishes.

_COOKIE_SITES = ("youtube.com", "google.com")
_COOKIES: dict = {}


def _cookie_site(domain: str) -> bool:
    domain = (domain or "").lower().lstrip(".")
    return any(domain == site or domain.endswith("." + site) for site in _COOKIE_SITES)


def _cookie_row(cookie) -> dict:
    """A cookie's values, copied out while the cookie is still there."""
    expiry = cookie.expirationDate()
    return {
        "domain": cookie.domain(),
        "path": cookie.path() or "/",
        "secure": cookie.isSecure(),
        "expiry": int(expiry.toSecsSinceEpoch()) if expiry.isValid() else 0,
        "name": bytes(cookie.name()).decode("utf-8", "replace"),
        "value": bytes(cookie.value()).decode("utf-8", "replace"),
    }


def watch_cookies(store) -> None:
    """Follow a profile's cookie store, keeping the YouTube and Google ones.

    The cookie the engine passes to cookieAdded is only lent for that call.
    Keeping the object itself and reading it later read freed memory and
    crashed, so its values are copied out at once.
    """
    def added(cookie):
        if _cookie_site(cookie.domain()):
            row = _cookie_row(cookie)
            _COOKIES[(row["name"], row["domain"], row["path"])] = row

    def removed(cookie):
        name = bytes(cookie.name()).decode("utf-8", "replace")
        _COOKIES.pop((name, cookie.domain(), cookie.path() or "/"), None)

    store.cookieAdded.connect(added)
    store.cookieRemoved.connect(removed)
    store.loadAllCookies()


def cookies_snapshot() -> list:
    """The kept cookies as plain values, safe to hand to another thread."""
    return [dict(row) for row in list(_COOKIES.values())]


def netscape_cookies(rows: list) -> str:
    """The cookie file format yt-dlp's --cookies reads."""
    lines = ["# Netscape HTTP Cookie File"]
    for row in rows:
        domain = row["domain"]
        lines.append("\t".join([
            domain, "TRUE" if domain.startswith(".") else "FALSE", row["path"],
            "TRUE" if row["secure"] else "FALSE", str(row["expiry"]),
            row["name"], row["value"]]))
    return "\n".join(lines) + "\n"


def cookie_header(rows: list, url: str) -> str:
    """A Cookie header of the rows that apply to url."""
    import urllib.parse

    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    pairs = []
    for row in rows:
        domain = row["domain"].lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            pairs.append(f"{row['name']}={row['value']}")
    return "; ".join(pairs)


def trace_stream(stream: str, agent: str = "", timeout: int = 15,
                 cookies: list = None) -> list:
    """Follow an HLS stream from its playlist down to a first segment.

    Returns one line per step, for the log: the playlist, the first quality
    level it lists, and that level's first piece of video. A stream can answer
    at the top and still be refused further down, and a player fed such a
    stream waits for a picture that never comes. This says where it stopped.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    headers = {"User-Agent": agent} if agent else {}
    lines = []

    def fetch(url, limit):
        sent = dict(headers)
        if cookies:
            jar = cookie_header(cookies, url)
            if jar:
                sent["Cookie"] = jar
        request = urllib.request.Request(url, headers=sent)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(limit)

    def first_entry(text, base):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return urllib.parse.urljoin(base, line)
        return ""

    url = stream
    for step in ("playlist", "quality level", "first segment"):
        try:
            status, body = fetch(url, 4096 if step == "first segment" else 1 << 20)
        except urllib.error.HTTPError as exc:
            lines.append(f"{step}: refused, HTTP {exc.code}")
            return lines
        except Exception as exc:                          # noqa: BLE001
            lines.append(f"{step}: unreachable, {exc}")
            return lines
        lines.append(f"{step}: HTTP {status}, {len(body)} bytes")
        if step == "first segment":
            return lines
        text = body.decode("utf-8", "replace")
        if not text.lstrip().startswith("#EXTM3U"):
            lines.append(f"{step}: not a playlist, begins {text[:30]!r}")
            return lines
        following = first_entry(text, url)
        if not following:
            lines.append(f"{step}: lists nothing to fetch")
            return lines
        # a media playlist lists segments, not levels: skip to the segment
        if step == "playlist" and "#EXT-X-STREAM-INF" not in text:
            lines.append("quality level: none, the playlist lists segments directly")
            url = following
            try:
                status, body = fetch(url, 4096)
                lines.append(f"first segment: HTTP {status}, {len(body)} bytes")
            except urllib.error.HTTPError as exc:
                lines.append(f"first segment: refused, HTTP {exc.code}")
            except Exception as exc:                      # noqa: BLE001
                lines.append(f"first segment: unreachable, {exc}")
            return lines
        url = following
    return lines


def _try_at_once(base: list, url: str, attempts, timeout: int, note) -> tuple:
    """Ask yt-dlp every way at the same time; the first answer wins.

    One after another, three ways of asking could take minutes when the
    first two failed slowly. At the same time, the wait is only as long as
    the quickest way that works, and the rest are stopped once one has.
    Returns (stream or "", reasons in the order tried, runtime missing).
    """
    import concurrent.futures

    procs: list = []

    def attempt(label, extra):
        try:
            done = _run_helper(base + extra + [url], timeout=timeout, record=procs)
        except subprocess.TimeoutExpired:
            return label, "", ["timed out"]
        except Exception as exc:                  # noqa: BLE001
            return label, "", [str(exc)]
        found = [line.strip() for line in done.stdout.splitlines()
                 if line.strip().startswith(("http://", "https://"))]
        if done.returncode == 0 and found:
            return label, found[0], []
        return label, "", _telling_lines(done.stderr or done.stdout or "")

    results = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(attempts))
    try:
        futures = [pool.submit(attempt, label, extra) for label, extra in attempts]
        for future in concurrent.futures.as_completed(futures):
            label, stream, telling = future.result()
            results[label] = telling
            if stream:
                note(f"stream lookup, {label}: found a stream")
                for proc in procs:                # the others are not needed now
                    if proc.poll() is None:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                return stream, [], False
            note(f"stream lookup, {label}: nothing usable")
            for line in telling:
                note(f"    {line}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    reasons, missing = [], False
    for label, _extra in attempts:
        telling = results.get(label, [])
        joined = " ".join(telling)
        if "JavaScript runtime" in joined or "challenge solving failed" in joined:
            missing = True
        reasons.append(f"{label}: {telling[-1] if telling else 'no stream found'}")
    return "", reasons, missing


def resolve_stream(url: str, timeout: int = 60, cookies: list = None) -> tuple[bool, str]:
    """The direct address of the stream on a page, or why there is none.

    Tries each way of asking in turn and returns the first address found.
    Every attempt, and what yt-dlp said about it, goes into merlin-log.txt,
    so a failure can be read in full rather than guessed at from its last
    line. Slow, since each attempt fetches the page: call it off the UI thread.
    """
    path = ytdlp_path()
    if not path:
        return False, "yt-dlp is not installed"
    base = _ytdlp_argv(path) + ["--get-url", "--no-playlist"]
    cookie_file = ""
    if cookies:
        cookie_file = _write_cookie_file(cookies)
        if cookie_file:
            base += ["--cookies", cookie_file]
    try:
        return _resolve(url, timeout, path, base)
    finally:
        if cookie_file:
            try:
                os.remove(cookie_file)
            except OSError:
                pass


def _write_cookie_file(rows: list) -> str:
    """A private, temporary cookie file for one yt-dlp lookup."""
    import tempfile

    try:
        handle, name = tempfile.mkstemp(prefix="merlin-yt-", suffix=".txt")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(netscape_cookies(rows))
        os.chmod(name, 0o600)
        return name
    except OSError:
        return ""


def _resolve(url: str, timeout: int, path: str, base: list) -> tuple[bool, str]:
    deno = deno_path()
    if deno:
        # named explicitly rather than left to be found: on Linux yt-dlp only
        # looks on the PATH, and Merlin's copy lives in its own folder
        base = base + ["--js-runtimes", f"deno:{deno}"]      # a new list: _resolve may run twice
    elif _needs_js_runtime(url):
        return False, JS_RUNTIME_MISSING

    try:
        from . import crashlog
        note = crashlog.note
    except Exception:                             # noqa: BLE001
        def note(_text):
            return None

    attempts = YOUTUBE_ATTEMPTS if _is_youtube(url) else OTHER_ATTEMPTS
    found_stream, reasons, runtime_missing = _try_at_once(
        base, url, attempts, timeout, note)
    if not found_stream and not runtime_missing and _is_youtube(url):
        # YouTube's refusals come and go: merlin-log.txt shows the same
        # stream refused, then found moments later. One more round, shortly.
        import time

        time.sleep(2)
        note("stream lookup: refused, asking once more")
        found_stream, reasons, runtime_missing = _try_at_once(
            base, url, attempts, timeout, note)
    if found_stream:
        return True, found_stream
    if runtime_missing and not deno:
        return False, JS_RUNTIME_MISSING

    # YouTube changes often and yt-dlp follows it, so a failure is often just
    # an out of date yt-dlp. Merlin's own copy can update itself: do that once
    # per session and try again, before giving up.
    global _UPDATED_THIS_SESSION
    own = os.path.join(ytdlp_folder(), _ytdlp_asset())
    if not _UPDATED_THIS_SESSION and os.path.abspath(path) == os.path.abspath(own):
        _UPDATED_THIS_SESSION = True
        # The nightly channel, not stable: YouTube's changes are usually met
        # there days before a stable release. Only Merlin's own copy is moved.
        note("stream lookup: every way failed, moving yt-dlp to its nightly "
             "build and trying again")
        try:
            updated = _run_helper(_ytdlp_argv(path) + ["--update-to", "nightly"],
                                  timeout=180)
            note("    " + (updated.stdout or updated.stderr).strip().splitlines()[-1]
                 if (updated.stdout or updated.stderr).strip() else "    (no output)")
        except Exception as exc:                  # noqa: BLE001
            note(f"    update failed: {exc}")
        else:
            # the same command, cookies included: the file is only deleted
            # once the outer call returns
            return _resolve(url, timeout, path, base)

    return False, "\n".join(r[:220] for r in reasons) or "no stream found"


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
