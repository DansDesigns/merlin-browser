#!/usr/bin/env python3
"""Exercise the code paths that only run on Windows.

A NameError inside an `if os.name == "nt"` block shipped once: QTimer was used
a few lines above its import, and every test skipped the branch. This forces
those paths to execute on any platform by pretending the Windows API is present
but inert, so a missing name or a bad call fails here rather than on a user's
machine.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu --disable-dev-shm-usage")

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  {status}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)


def fake_windows_api():
    """A stand-in for ctypes.windll that records calls instead of making them."""
    calls = []

    class FakeFunc:
        def __init__(self, name, result=1):
            self.name, self.result = name, result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    class FakeLib:
        def __init__(self, name):
            self._name = name
            self._funcs = {}

        def __getattr__(self, item):
            if item not in self._funcs:
                self._funcs[item] = FakeFunc(item)
            return self._funcs[item]

    class FakeWinDLL:
        user32 = FakeLib("user32")
        shell32 = FakeLib("shell32")

    return FakeWinDLL(), calls


print("Windows-only startup paths")

import ctypes

from merlin import winicon
from merlin.brand import icon_path

path = icon_path()
check("logo file ships with the package", bool(path), path)

# --- winicon under a simulated Windows ---
real_name, real_windll = os.name, getattr(ctypes, "windll", None)
fake, calls = fake_windows_api()
try:
    os.name = "nt"
    ctypes.windll = fake
    if not hasattr(ctypes, "wintypes"):
        import ctypes.wintypes  # noqa: F401

    applied = winicon.apply_to_window(types.SimpleNamespace(winId=lambda: 12345),
                                      path)
    check("apply_to_window runs without error", applied is True)
    names = [c[0] for c in calls]
    check("LoadImageW called for both sizes", names.count("LoadImageW") == 2,
          f"calls={names.count('LoadImageW')}")
    check("SendMessageW pushes WM_SETICON", names.count("SendMessageW") == 2)
    icons = sorted(c[1][2] for c in calls if c[0] == "SendMessageW")
    check("both ICON_SMALL and ICON_BIG set", icons == [0, 1], str(icons))
    check("describe() reports success", "successfully" in winicon.describe(path))
finally:
    os.name = real_name
    if real_windll is None:
        del ctypes.windll
    else:
        ctypes.windll = real_windll

check("no-op again off Windows",
      winicon.apply_to_window(types.SimpleNamespace(winId=lambda: 1), path) is False)

# --- every name used in main() resolves, in order ---
import ast

source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "merlin", "app.py")).read()
tree = ast.parse(source)
main_fn = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "main")

imported_at = {}
used_at = {}
for node in ast.walk(main_fn):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            name = (alias.asname or alias.name).split(".")[0]
            imported_at.setdefault(name, node.lineno)
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        used_at.setdefault(node.id, node.lineno)

late = [n for n, line in imported_at.items()
        if n in used_at and used_at[n] < line]
check("nothing in main() is used before it is imported", not late, str(late))

# --- icon resource structures, which the installer writes into Merlin.exe ---
from merlin.winexe import build_group, parse_ico

with open(path, "rb") as fh:
    ico = fh.read()
images = parse_ico(ico)
check("icon parses into images", len(images) >= 5, f"{len(images)} entries")
check("payload sizes match the directory",
      all(len(payload) == entry["size"] for entry, payload in images))
group = build_group(images)
check("group directory is 6 + 14n bytes", len(group) == 6 + 14 * len(images))
rejected = 0
for blob in (b"", b"\x01\x00\x02\x00\x01\x00", ico[:10], ico[:len(ico)//2]):
    try:
        parse_ico(blob)
    except ValueError:
        rejected += 1
check("malformed icons are rejected, not crashed on", rejected == 4)

# --- web app shortcut text is built without touching the filesystem ---
from merlin import webapps

check("slugify strips path-hostile characters",
      webapps.slugify("a/b c:d") == "a-b-c-d", webapps.slugify("a/b c:d"))
check("powershell literal escapes quotes",
      webapps._ps("it's") == "'it''s'", webapps._ps("it's"))

# --- proxy flags must survive being packed into one env var -----------------
from merlin import privacy

flags = privacy.proxy_flags("socks5://127.0.0.1:9050")
check("proxy flags contain no spaces inside a value",
      all(" " not in f for f in flags), str(flags))
check("no proxy flags when no proxy", privacy.proxy_flags("") == [])
check("tor url is empty when no daemon is running",
      privacy.tor_proxy_url(0) == "" or privacy.find_tor_port() != 0)

# --- web app shortcuts are passed to PowerShell without a temp file ---------
import base64

from merlin import webapps

sample = "$link.Arguments = " + webapps._ps('"C:\\a b\\run.py" --app "https://x"')
encoded = base64.b64encode(sample.encode("utf-16-le")).decode("ascii")
check("encoded command round trips",
      base64.b64decode(encoded).decode("utf-16-le") == sample)

import re as _re

root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# version.txt is maintained separately and must never be shipped: a stale one
# would override the real version wherever this is unpacked.
check("version.txt is not shipped",
      not os.path.exists(os.path.join(
          os.path.dirname(os.path.abspath(__file__)), "..", "version.txt")))

# Two fixes have gone missing between edits and shipped reverted. These check
# the behaviour is actually in the file rather than only in a passing test run.
updater_src = open(os.path.join(root, "merlin", "updater.py"),
                   encoding="utf-8").read()
check("updater does not refuse to update a built copy",
      "cannot be swapped out" not in updater_src)
check("updater finds the app folder on disk",
      "target = writable_app_dir()" in updater_src)

browser_src = open(os.path.join(root, "merlin", "browser.py"),
                   encoding="utf-8").read()
save_session = browser_src.split("def save_session")[1][:900]
check("session save falls back to a tab's pending address",
      "pending_url" in save_session)

browser_src2 = open(os.path.join(root, "merlin", "browser.py"),
                    encoding="utf-8").read()
check("deferred callbacks check the view still exists",
      "def view_is_alive" in browser_src2
      and browser_src2.count("view_is_alive(view)") >= 2)
# the whole function, not a fixed number of characters into it: a longer
# comment should not be able to make this fail
_close_tab = browser_src2.split("def close_tab")[1].split("\n    def ")[0]
check("closing a tab stops its load first", "view.stop()" in _close_tab)

check("https fallback retries on http after a failed upgrade",
      "def _retry_without_upgrade" in browser_src2
      and "_retry_without_upgrade(view, ok)" in browser_src2)

adblock_src = open(os.path.join(root, "merlin", "adblock.py"),
                   encoding="utf-8").read()
check("local addresses are exempt from the https upgrade",
      "def is_local_host" in adblock_src and "is_private" in adblock_src)

# merlin-run.py is frozen into Merlin.exe, so anything that has to reach an
# installed copy through an update must live in the package instead.
app_src = open(os.path.join(root, "merlin", "app.py"), encoding="utf-8").read()
check("crash log lives in the package, where updates reach it",
      os.path.isfile(os.path.join(root, "merlin", "crashlog.py"))
      and "crashlog.enable()" in app_src)

# A no-argument disconnect() on a web view also cuts Qt WebEngine's own
# internal connections, and the next restyle of the application crashes.
import re as _re_dc
check("no wildcard disconnect on a web view",
      not _re_dc.search(r"^\s*view\.disconnect\(\)", browser_src2, _re_dc.M))

bat_src = open(os.path.join(root, "install.bat"), encoding="utf-8").read()
check("Qt Multimedia is bundled into Merlin.exe",
      "--hidden-import PyQt6.QtMultimedia " in bat_src
      and "--hidden-import PyQt6.QtMultimediaWidgets" in bat_src)

check("the runtime retry cannot loop",
      "JS_RUNTIME_MISSING and not media.deno_path()" in browser_src2)

# Merlin.exe only holds the standard modules it was built with, and the
# package updates from disk. Every standard module the package imports must
# be in the anchor, or an update can fail on Windows with no sign of it here.
import ast as _ast
_anchor_src = open(os.path.join(root, "merlin", "stdlib_anchor.py"),
                   encoding="utf-8").read()
# Full dotted names, and every subfolder: importing a package does not bring
# its submodules (html does not bring html.parser, which MerlinEngine needs),
# and merlin/engine/ is a folder of its own. Comparing only top-level names
# in the top folder had missed both.
_anchored = {a.name for n in _ast.walk(_ast.parse(_anchor_src))
             if isinstance(n, _ast.Import) for a in n.names}
_used = set()
for _base, _dirs, _files in os.walk(os.path.join(root, "merlin")):
    for _name in _files:
        if not _name.endswith(".py") or _name == "stdlib_anchor.py":
            continue
        _tree = _ast.parse(open(os.path.join(_base, _name), encoding="utf-8").read())
        for _node in _ast.walk(_tree):
            if isinstance(_node, _ast.Import):
                _mods = [a.name for a in _node.names]
            elif isinstance(_node, _ast.ImportFrom) and _node.module and _node.level == 0:
                _mods = [_node.module]
            else:
                continue
            for _m in _mods:
                if _m.split(".")[0] in sys.stdlib_module_names and _m != "os.path":
                    _used.add(_m)
_missing = sorted(_used - _anchored)
check("every standard module the package uses is carried in Merlin.exe",
      not _missing, ", ".join(_missing))
check("install.bat bundles the standard-library anchor",
      "--hidden-import merlin.stdlib_anchor" in bat_src)

check("shutdown is bounded by a watchdog",
      "watchdog = threading.Timer(6.0, give_up)" in app_src
      and "app.aboutToQuit.connect" in app_src)

check("a clean shutdown skips interpreter teardown, after saving",
      "def _leave(" in app_src and "os._exit(code)" in app_src
      and "settings.save()" in app_src.split("def _leave(")[1][:2000])
check("view checks ask sip whether the object still exists",
      "sip.isdeleted(view)" in browser_src2)
check("the installer puts in a codec engine on every install",
      "use-codec-engine.py" in bat_src)

# The codec step is never fatal: set -e must not see its exit code, and the
# batch file must not leave its error level for a later check to misread.
sh_src = open(os.path.join(root, "install.sh"), encoding="utf-8").read()
check("the codec step can never stop the Linux install",
      "|| codec_rc=$?" in sh_src)
_codec_block = bat_src.split("use-codec-engine.py\" --auto")[1][:900]
check("the codec step leaves no error level behind on Windows",
      "ver >nul" in _codec_block)
check("both installers fetch the codec engine automatically",
      "use-codec-engine.py\" --auto" in bat_src
      and "use-codec-engine.py\" --auto" in sh_src)

# MerlinSetup.exe is install.bat's own folder once unpacked, so every file the
# installer reads from %SRC% has to be bundled into it. When tools\ was left
# out, the codec engine step silently never ran for anyone using the setup.
_setup_src = open(os.path.join(root, "tools", "build-installer.bat"),
                  encoding="utf-8").read()
_bundled = _re.findall(r'--add-data "%SRC%\\([^;"]+);', _setup_src)
_read = sorted(set(_re.findall(r'%SRC%\\([A-Za-z0-9_.\-\\]+)', bat_src)))
_uncarried = [p for p in _read
              if not any(p == b or p.startswith(b + "\\") for b in _bundled)]
check("MerlinSetup.exe carries every file install.bat reads",
      not _uncarried, ", ".join(_uncarried))

# The engine must match the Qt actually running (qVersion), not the version
# PyQt6 was compiled against (QT_VERSION_STR): in PyQt6 6.11 those differ.
_wf_only = open(os.path.join(root, ".github", "workflows", "build-webengine-codecs.yml"),
                encoding="utf-8").read()
_part = open(os.path.join(root, ".github", "actions", "codec-build-part", "action.yml"),
             encoding="utf-8").read()
# the build steps live in the shared part; the checks read both together
_wf = _wf_only + "\n" + _part
# GitHub stops a job at six hours and the build needs longer: it runs in
# parts, each carrying on from the last, and the tools stay at fixed paths so
# a resumed build finds them where the first part did.
check("the engine build runs in parts that carry on from each other",
      all(f"previous-done: ${{{{ needs.part{i - 1}.outputs.done }}}}" in _wf_only
          for i in range(2, 6)) and "timeout-minutes: 355" in _wf_only)
check("each part's tools are at fixed paths",
      'python-version: "3.12.10"' in _part and "lukka/get-cmake" not in _wf
      and 'CMAKE_MAKE_PROGRAM="C:\\ProgramData\\chocolatey\\bin\\ninja.exe"' in _part
      and "C:\\tools\\cmake-3.29.6-windows-x86_64" in _part)
check("a finished build is not mistaken for a failed one",
      "$null = $p.Handle" in _part
      and _part.index("$null = $p.Handle") < _part.index("while (-not $p.HasExited)"))
check("the engine workflow builds the running Qt, not the compiled-against one",
      "qVersion()" in _wf and "QT_VERSION_STR" not in _wf)
check("the workflow checks H.264 plays before it publishes",
      _wf.index("use-codec-engine.py \"C:\\engine\"")
      < _wf.index("--package"))
# Chromium's gn relates source and build paths, and Windows has no relative
# path between drives: the first real run failed on exactly that.
_cfg = [l for l in _wf.splitlines() if l.strip().startswith(("cmake \"", "-DCMAKE_INSTALL_PREFIX", "mkdir C:", "mkdir -p /c/"))]
check("the workflow keeps source, build and output on one drive",
      'cmake "C:\\s\\qtwebengine"' in _wf and "mkdir C:\\b" in _wf
      and "mkdir -p /c/s" in _wf and '-DCMAKE_INSTALL_PREFIX="C:\\engine"' in _wf
      and '--outputdir "C:\\Qt"' in _wf, str(_cfg))
check("the workflow asks for the proprietary codecs",
      "-DQT_FEATURE_webengine_proprietary_codecs=ON" in _wf)
check("the workflow keeps WebRTC, PDF and the spellchecker",
      not any(f"webengine_{f}=OFF" in _wf
              for f in ("webrtc", "printing_and_pdf", "spellchecker", "extensions")))

# Merlin applies a staged engine at start-up. That only works before the
# engine is loaded: afterwards Windows will not let it be replaced.
_main = app_src.split("def main(")[1]
check("a staged engine is applied before the engine is first imported",
      _main.index("codecengine.apply_staged") < _main.index("from PyQt6.QtWebEngineCore"))
check("the probe child is answered before the log and single instance",
      _main.index('"--probe-codecs"') < _main.index("crashlog.enable()")
      and _main.index('"--probe-codecs"') < _main.index("single.hand_off"))
_ce = open(os.path.join(root, "merlin", "codecengine.py"), encoding="utf-8").read()
check("engine files are replaced by rename, never written in place",
      "os.replace(fresh, destination)" in _ce
      and "_replace_file(os.path.join(prefix" in _ce)
check("a staged engine waits while another Merlin has the engine open",
      _ce.index("engine_in_use(qt_folder())") < _ce.index('version = staged.get("version"'))

_play = browser_src2.split("    def play_stream(")[1].split("\n    def ")[0]
# The page is asked, several times, and yt-dlp is only the branch taken when
# it still has no stream.
_ask = browser_src2.split("    def _ask_page_for_stream(")[1].split("\n    def ")[0]
check("playing a stream asks the page before yt-dlp",
      "self._ask_page_for_stream(" in _play
      and _ask.index("if stream:") < _ask.index("elif tries_left > 1:")
      < _ask.index("self._play_with_ytdlp(page)"))
_detect = browser_src2.split("    def _check_live_stream(")[1].split("\n    def ")[0]
check("a stream the engine cannot play opens in the player by itself",
      "self.play_stream(page)" in _detect and "show_notice" not in _detect)

_inplace_src = open(os.path.join(root, "merlin", "inplace.py"), encoding="utf-8").read()
check("the in-page player's worker is cut loose before it stops",
      _inplace_src.index("signal.disconnect()") < _inplace_src.index("self._ask_shut_down.emit()"))
_resolved = browser_src2.split("    def _on_stream_resolved(")[1].split("\n    def ")[0]
check("a found stream plays over its page rather than in a tab",
      "_play_in_page(view, result, page, source)" in _resolved)
_loadstate = browser_src2.split("    def _on_load_state(")[1].split("\n    def ")[0]
check("a reload looks for an unplayable stream again",
      "_schedule_live_check(view)" in _loadstate
      and "_offered_streams.discard" in _loadstate)

# Qt sends events through an event filter while its window is destroyed, after
# Python has let go of the filter's attributes. The tab container's filter
# raised there at random and stopped the test suite; each filter guards it.
def _filter_body(path):
    text = open(os.path.join(root, "merlin", path), encoding="utf-8").read()
    return text.split("def eventFilter(")[1][:700]
check("event filters stand aside while their object is destroyed",
      'getattr(self, "stack", None)' in _filter_body("tabs.py")
      and "except (AttributeError, RuntimeError)" in _filter_body("gestures.py")
      and 'getattr(self, "_full_screen", False)' in _filter_body("inplace.py")
      and 'getattr(self, "url_bar", None)' in _filter_body("browser.py"))

# A tab's view may be either engine's. Checks meaning "a web page tab" accept
# both; only the ones that run JavaScript in the page need Chromium itself.
def _checks_in(name):
    body = browser_src2.split(f"    def {name}(")[1].split("\n    def ")[0]
    return ("PAGE_VIEWS" in body, "isinstance(view, WebView)" in body)
check("tab checks accept either engine's tab",
      all(_checks_in(n)[0] for n in ("current", "close_tab", "save_session",
                                     "release_pages", "refresh_start_pages")))
check("only the in-page player paths require Chromium",
      _checks_in("play_stream")[1] and _checks_in("_on_stream_resolved")[1])

# --- batch quoting hazards --------------------------------------------------
# A PowerShell call with \" escapes inside a for /f broke install.bat twice:
# cmd has no backslash escape, so the quotes ended the string early and the
# rest of the line was parsed as commands.
def batch_quoting_problems(text):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("rem") or not stripped:
            continue
        # `if "%VAR:~-1%"=="\"` is the trailing-backslash idiom, not an escape
        if '\\"' in line and '=="\\"' not in line.replace(" ", ""):
            found.append((number, "backslash-escaped quote"))
        if "for /f" in line:
            inner = line.split("(", 1)[-1]
            for match in _re.finditer(r"\^(.)", inner):
                if match.group(1) not in "<>&|^":
                    found.append((number, f"caret eats {match.group(1)!r}"))
    return found


for name in ("install.bat", "uninstall.bat"):
    text = open(os.path.join(root, name), encoding="utf-8").read()
    issues = batch_quoting_problems(text)
    check(f"{name} has no cmd quoting hazards", not issues, str(issues))

# Nothing shipped may delete, rewrite or restart anything it does not own. An
# installer that stamped its identity onto every pinned shortcut merged other
# applications into Merlin and replaced their icons.
for name in ("install.bat", "uninstall.bat"):
    text = open(os.path.join(root, name), encoding="utf-8").read()
    commands = "\n".join(l for l in text.splitlines()
                         if not l.strip().lower().startswith("rem"))

    # reading the pinned folder is fine; writing to a shortcut that is not
    # ours is not, so any write must be guarded by both of these conditions
    if "User Pinned" in commands:
        # the only write is clearing our own id, and only from shortcuts that
        # actually carry it
        check(f"{name} only touches shortcuts carrying Merlin's own id",
              "$current -eq $id" in commands)
        check(f"{name} never sets an application id on a shortcut",
              "v.vt = 31" not in commands and "Tag]::Apply" not in commands)
        # writing an id to our own named shortcuts is fine; writing to one
        # that came out of a listing is what damaged other applications
        writes = [l for l in commands.splitlines()
                  if "Tag]::Apply" in l or "Stamp]::Apply" in l]
        check(f"{name} never tags an enumerated shortcut",
              not any("Get-ChildItem" in l or "$_." in l for l in writes),
              str(writes)[:90])
        check(f"{name} tags only shortcuts it names itself",
              all("Merlin Browser" in l or "$own" in l or "$lnk" in l
                  for l in writes) if writes else True)
    # deleting a shortcut we created by name is fine; deleting one that came
    # out of a directory listing means deleting somebody else's
    enumerated = _re.findall(r'Remove-Item\s+(\$_[^\s;)]*)', commands)
    piped = [l for l in commands.splitlines()
             if "Remove-Item" in l and "Get-ChildItem" in l]
    check(f"{name} never deletes an enumerated shortcut",
          not enumerated and not piped, str(enumerated or piped)[:80])

    # process control: only Merlin's own process, and never the shell
    kills = _re.findall(r'taskkill[^\n]*?/im\s+(\S+)', commands)
    check(f"{name} only ever closes Merlin.exe",
          all(k.lower().startswith("merlin") for k in kills), str(kills))
    check(f"{name} never starts or stops Explorer",
          "start explorer" not in commands.lower()
          and not _re.search(r'taskkill[^\n]*explorer', commands, _re.I))

# --- installer variables must be set before they are used -------------------

def first_use_before_set(text, names, set_pattern, use_pattern):
    late = []
    for name in names:
        sets = [m.start() for m in _re.finditer(set_pattern.format(n=name), text)]
        uses = [m.start() for m in _re.finditer(use_pattern.format(n=name), text)]
        if uses and (not sets or min(uses) < min(sets)):
            late.append(name)
    return late


bat = open(os.path.join(root, "install.bat"), encoding="utf-8").read()
bat_names = sorted(set(_re.findall(r'set "([A-Z_][A-Z0-9_]*)=', bat)))
late_bat = first_use_before_set(
    bat, bat_names, r'set "{n}=', r'%{n}%')
# CUR_STEP and CUR_LABEL are written by :step and read by :bar, which is a
# subroutine defined after them; that ordering is fine.
late_bat = [n for n in late_bat if n not in ("CUR_STEP", "CUR_LABEL", "ESC")]
check("install.bat sets every variable before using it", not late_bat,
      str(late_bat))

# install.sh is executed during testing, so a syntax check is enough here;
# install.bat cannot be run on this platform, which is why it gets the static
# variable-order check above. That check exists because an empty %RUNPY% was
# passed to the icon step, and python read the working directory as a script.
import subprocess as _sp

sh_path = os.path.join(root, "install.sh")
syntax = _sp.run(["bash", "-n", sh_path], capture_output=True)
check("install.sh parses", syntax.returncode == 0,
      syntax.stderr.decode()[:120])

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all startup paths OK")
