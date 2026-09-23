"""Qt WebEngine with H.264 and AAC: fetching it, checking it, swapping it in.

Merlin's engine comes from the PyQt6 wheels, built without H.264 and AAC, the
codecs YouTube live uses. A build of the same Qt WebEngine version with them is
published on the Merlin repository, made by .github/workflows/build-webengine-
codecs.yml, and this module puts it in place. One implementation, used by:

  the installers      through tools/use-codec-engine.py --auto
  the workflow        through tools/use-codec-engine.py, to check and package
  Merlin itself       stage() now, apply_staged() at the next start

Nothing is kept unless it passes. Downloads are checked against their published
SHA-256 before being unpacked, archives may not write outside their folder, the
original engine is backed up first, and the new one is started in a separate
process and asked whether it plays H.264 and AAC. If it will not start or says
no, the original goes straight back.

Only standard modules that every existing Merlin.exe already carries are used:
zipfile, json and urllib.request directly, hashlib by way of urllib.request
and tarfile by way of shutil. A module an older executable lacks would fail
exactly on the installs this exists to reach.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

BACKUP = "merlin-original-engine"

DEFAULT_RELEASES = "https://github.com/DansDesigns/merlin-browser/releases/download"


def releases_base() -> str:
    return os.environ.get("MERLIN_ENGINE_URL", DEFAULT_RELEASES).rstrip("/")


def asset_name(version: str) -> str:
    """The release asset holding the engine for this platform and version."""
    if os.name == "nt":
        return f"qtwebengine-{version}-codecs-windows-x64.zip"
    machine = os.uname().machine.lower()
    arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
    return f"qtwebengine-{version}-codecs-linux-{arch}.tar.gz"


def asset_url(version: str) -> str:
    return f"{releases_base()}/engine-{version}/{asset_name(version)}"


# Run in a separate process, so the engine being checked is the one on disk
# now rather than whichever was loaded before the swap. Writes its answer to
# the file named as its last argument: a windowed Merlin.exe has no stdout.
PROBE_SCRIPT = r"""
import json, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
sys.path.insert(0, os.environ.get("MERLIN_PROBE_PATH", ""))
from merlin.codecengine import probe_here
probe_here(sys.argv[-1])
"""


def probe_here(outfile: str) -> None:
    """Ask the engine in this process what it plays, and write the answer.

    Used by the probe child, whether that is python running PROBE_SCRIPT or
    Merlin.exe started with --probe-codecs.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWebEngineCore import qWebEngineChromiumVersion, qWebEngineVersion
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])
    view = QWebEngineView()
    view.setHtml("<video id=v></video>")
    out = {"engine": qWebEngineVersion(), "chromium": qWebEngineChromiumVersion()}

    def done(result):
        out["h264"], out["aac"], out["mse_h264"] = result or ["", "", False]
        with open(outfile, "w", encoding="utf-8") as handle:
            json.dump(out, handle)
        app.quit()

    def ask():
        view.page().runJavaScript(
            "var v=document.getElementById('v');"
            "[v.canPlayType('video/mp4; codecs=\"avc1.4d401e\"'),"
            " v.canPlayType('audio/mp4; codecs=\"mp4a.40.2\"'),"
            " MediaSource.isTypeSupported('video/mp4; codecs=\"avc1.4d401e\"')]",
            done)

    QTimer.singleShot(1500, ask)
    QTimer.singleShot(30000, app.quit)
    app.exec()


def probe_command(outfile: str, python: str = "") -> list:
    """How to start a probe child for the engine this copy of Merlin loads."""
    if python:
        return [python, "-c", PROBE_SCRIPT, outfile]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--probe-codecs", outfile]
    runner = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "merlin-run.py")
    return [sys.executable, runner, "--probe-codecs", outfile]


def probe(python: str = "") -> dict:
    """Start the engine in its own process and ask what it can play."""
    import tempfile

    handle, outfile = tempfile.mkstemp(prefix="merlin-probe-", suffix=".json")
    os.close(handle)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               MERLIN_PROBE_PATH=os.path.dirname(os.path.dirname(
                   os.path.abspath(__file__))))
    try:
        done = subprocess.run(probe_command(outfile, python), capture_output=True,
                              text=True, timeout=120, env=env,
                              **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        try:
            with open(outfile, encoding="utf-8") as result:
                return json.load(result)
        except (OSError, ValueError):
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
            return {"error": "the engine would not start: " + " | ".join(tail)}
    except subprocess.TimeoutExpired:
        return {"error": "the engine did not answer within two minutes"}
    finally:
        try:
            os.remove(outfile)
        except OSError:
            pass


def qt_folder() -> str:
    """The Qt folder this PyQt6 loads from, in a venv or inside Merlin.exe."""
    import PyQt6

    return os.path.join(os.path.dirname(PyQt6.__file__), "Qt6")


def engine_files(root: str, installed: bool) -> list:
    """The files that make up Qt WebEngine, relative to a Qt folder.

    installed=True describes PyQt6's own layout; False describes a Qt install
    prefix, as a build produces. They differ only on Linux, where the wheel
    names the library without its full version suffix.
    """
    files = []
    if os.name == "nt":
        files += [os.path.join("bin", "Qt6WebEngineCore.dll"),
                  os.path.join("bin", "QtWebEngineProcess.exe")]
    else:
        lib = os.path.join(root, "lib")
        names = sorted(n for n in os.listdir(lib) if n.startswith("libQt6WebEngineCore.so")) \
            if os.path.isdir(lib) else []
        if installed:
            files.append(os.path.join("lib", "libQt6WebEngineCore.so.6"))
        elif names:
            # the real file, not a symlink to it
            real = [n for n in names if not os.path.islink(os.path.join(lib, n))]
            files.append(os.path.join("lib", (real or names)[-1]))
        else:
            # Always a first entry, found or not. The first two entries are
            # paired with PyQt6's own by position, so a missing library must
            # still hold its place: otherwise the helper process would be
            # paired with, and copied over, the engine library.
            files.append(os.path.join("lib", "libQt6WebEngineCore.so.6"))
        files.append(os.path.join("libexec", "QtWebEngineProcess"))
    resources = os.path.join(root, "resources")
    if os.path.isdir(resources):
        for name in sorted(os.listdir(resources)):
            if os.path.isfile(os.path.join(resources, name)):
                files.append(os.path.join("resources", name))
    return files


def has_codecs(result: dict) -> bool:
    return bool(result.get("h264")) and bool(result.get("aac")) \
        and bool(result.get("mse_h264"))


def describe(result: dict) -> str:
    if "error" in result:
        return result["error"]
    chromium = f", Chromium {result['chromium']}" if result.get("chromium") else ""
    return (f"Qt WebEngine {result.get('engine')}{chromium}: "
            f"H.264 {result.get('h264') or 'no'}, AAC {result.get('aac') or 'no'}, "
            f"H.264 for streaming {'yes' if result.get('mse_h264') else 'no'}")


def prefix_version(prefix: str) -> str:
    """The Qt WebEngine version a build prefix holds, from its CMake files."""
    path = os.path.join(prefix, "lib", "cmake", "Qt6WebEngineCore",
                        "Qt6WebEngineCoreConfigVersion.cmake")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    import re

    found = re.search(r'set\(PACKAGE_VERSION\s+"([\d.]+)"\)', text)
    return found.group(1) if found else ""


def _replace_file(source: str, destination: str) -> None:
    """Copy source over destination by writing beside it and renaming.

    Never writes into the existing file. On Linux a process that has the old
    engine loaded, a web app window for instance, keeps the old file and does
    not crash; on Windows a file in use makes this fail cleanly instead of
    leaving half a file behind.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    fresh = destination + ".merlin-new"
    shutil.copy2(source, fresh)
    try:
        os.replace(fresh, destination)
    except OSError:
        try:
            os.remove(fresh)
        except OSError:
            pass
        raise


def engine_in_use(target: str) -> bool:
    """Whether another process has this engine loaded, on Windows.

    Windows will not let a loaded library be replaced. Merlin's web apps run
    as processes of their own, so one may hold the engine while another starts.
    """
    if os.name != "nt":
        return False
    library = os.path.join(target, engine_files(target, installed=True)[0])
    try:
        with open(library, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def restore(target: str, say=print) -> bool:
    backup = os.path.join(target, BACKUP)
    if not os.path.isdir(backup):
        say("No original engine is backed up, so there is nothing to restore.")
        return False
    failed = []
    for base, _dirs, names in os.walk(backup):
        for name in names:
            source = os.path.join(base, name)
            relative = os.path.relpath(source, backup)
            try:
                _replace_file(source, os.path.join(target, relative))
            except OSError:
                failed.append(relative)
    if failed:
        # the backup is kept, so the next attempt can finish the job
        say("Some original files could not be put back yet: " + ", ".join(failed))
        return False
    shutil.rmtree(backup)
    say("The original engine is back in place.")
    return True


def swap_in(prefix: str, target: str, check, say=print, before: dict = None) -> bool:
    """Put the build in prefix into target, and keep it only if check passes.

    check() starts the engine in a separate process and returns what it
    reports. before, if given, is what the engine in place reported already,
    which saves starting it a second time.
    """
    before = before if before is not None else check()
    say("In use now: " + describe(before))
    if has_codecs(before):
        say("This engine already plays H.264 and AAC. Nothing to do.")
        return True

    wanted = before.get("engine", "")
    offered = prefix_version(prefix)
    if offered and wanted and offered != wanted:
        say(f"That build is Qt WebEngine {offered}, but this PyQt6 uses {wanted}. "
            "They have to match exactly.")
        return False

    source_files = engine_files(prefix, installed=False)
    target_files = engine_files(target, installed=True)
    missing = [f for f in source_files[:2]
               if not os.path.isfile(os.path.join(prefix, f))]
    if missing:
        say("That folder does not hold a Qt WebEngine build. Missing: "
            + ", ".join(os.path.join(prefix, name) for name in missing))
        return False
    absent = [f for f in target_files[:2] if not os.path.isfile(os.path.join(target, f))]
    if absent:
        say("The engine in use is not where expected, so it is left alone: "
            + ", ".join(os.path.join(target, name) for name in absent))
        return False

    backup = os.path.join(target, BACKUP)
    if os.path.isdir(backup):
        say("An original engine is already backed up; keeping that one.")
    else:
        for relative in target_files:
            source = os.path.join(target, relative)
            if os.path.isfile(source):
                destination = os.path.join(backup, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
        locales = os.path.join(target, "translations", "qtwebengine_locales")
        if os.path.isdir(locales):
            shutil.copytree(locales, os.path.join(
                backup, "translations", "qtwebengine_locales"))

    try:
        # the library and helper process, mapped to PyQt6's own names
        for source_rel, target_rel in zip(source_files[:2], target_files[:2]):
            _replace_file(os.path.join(prefix, source_rel),
                          os.path.join(target, target_rel))
        for relative in source_files[2:]:
            _replace_file(os.path.join(prefix, relative),
                          os.path.join(target, relative))
        locales = os.path.join(prefix, "translations", "qtwebengine_locales")
        if os.path.isdir(locales):
            for name in os.listdir(locales):
                _replace_file(os.path.join(locales, name), os.path.join(
                    target, "translations", "qtwebengine_locales", name))
    except OSError as exc:
        say(f"Copying the new engine in failed: {exc}")
        restore(target, say)
        return False

    after = check()
    say("After the swap: " + describe(after))
    if has_codecs(after):
        shutil.rmtree(backup, ignore_errors=True)
        say("Done: this engine plays H.264 and AAC.")
        return True
    say("That engine did not pass, so the original is going back.")
    restore(target, say)
    return False


def _download(url: str, destination: str) -> tuple:
    """Fetch url to destination. (True, "") or (False, why); 404 is its own."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "Merlin Browser"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, \
                open(destination, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            shown = -1
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total > (1 << 20):             # not for the checksum file
                    percent = done * 100 // total
                    if percent // 10 != shown:
                        shown = percent // 10
                        print(f"        {percent:3d}%  {done >> 20} of {total >> 20} MB",
                              flush=True)
    except urllib.error.HTTPError as exc:
        return False, "missing" if exc.code == 404 else f"HTTP {exc.code}"
    except Exception as exc:                              # noqa: BLE001
        return False, str(exc)
    return True, ""


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_published(version: str, cache: str, say=print) -> tuple:
    """Download, check and unpack the published build. (prefix, message)."""
    import tarfile
    import zipfile

    os.makedirs(cache, exist_ok=True)
    name = asset_name(version)
    archive = os.path.join(cache, name)
    url = asset_url(version)
    say(f"Looking for a published build: {name}")

    ok, why = _download(url + ".sha256", archive + ".sha256")
    if not ok:
        if why == "missing":
            return "", (f"No build of Qt WebEngine {version} with the codecs has "
                        "been published yet.")
        return "", f"Could not reach the published builds: {why}"
    expected = open(archive + ".sha256", encoding="utf-8").read().split()[0].lower()

    if not (os.path.isfile(archive) and _sha256(archive) == expected):
        say("Downloading it...")
        ok, why = _download(url, archive)
        if not ok:
            return "", f"The download failed: {why}"
    actual = _sha256(archive)
    if actual != expected:
        os.remove(archive)
        return "", ("The download did not match its published checksum, so it "
                    "was thrown away rather than used.")

    prefix = os.path.join(cache, f"qtwebengine-{version}")
    if os.path.isdir(prefix):
        shutil.rmtree(prefix)
    os.makedirs(prefix)
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                _safe_extract_zip(bundle, prefix)
        else:
            with tarfile.open(archive) as bundle:
                _safe_extract_tar(bundle, prefix)
    except Exception as exc:                              # noqa: BLE001
        return "", f"Could not unpack the build: {exc}"
    return prefix, "downloaded and checked"


def _inside(base: str, path: str) -> bool:
    base = os.path.realpath(base)
    return os.path.realpath(path).startswith(base + os.sep)


def _safe_extract_zip(bundle, destination: str) -> None:
    for member in bundle.namelist():
        if not _inside(destination, os.path.join(destination, member)):
            raise ValueError(f"unsafe path in archive: {member}")
    bundle.extractall(destination)


def _safe_extract_tar(bundle, destination: str) -> None:
    for member in bundle.getmembers():
        if not _inside(destination, os.path.join(destination, member.name)):
            raise ValueError(f"unsafe path in archive: {member.name}")
        if member.issym() or member.islnk():
            link = os.path.join(destination, os.path.dirname(member.name),
                                member.linkname)
            if not _inside(destination, link):
                raise ValueError(f"unsafe link in archive: {member.name}")
    bundle.extractall(destination)


def package(prefix: str, outdir: str) -> int:
    """Turn a codec-enabled build into the release asset installers fetch."""
    import tarfile
    import zipfile

    version = prefix_version(prefix)
    if not version:
        print("That folder has no Qt WebEngine CMake files to say its version.")
        return 1
    files = [f for f in engine_files(prefix, installed=False)
             if os.path.isfile(os.path.join(prefix, f))]
    if len(files) < 2:
        print("That folder does not hold a complete Qt WebEngine build.")
        return 1
    version_file = os.path.join("lib", "cmake", "Qt6WebEngineCore",
                                "Qt6WebEngineCoreConfigVersion.cmake")
    files.append(version_file)
    locales = os.path.join(prefix, "translations", "qtwebengine_locales")
    if os.path.isdir(locales):
        for name in sorted(os.listdir(locales)):
            files.append(os.path.join("translations", "qtwebengine_locales", name))

    os.makedirs(outdir, exist_ok=True)
    name = asset_name(version)
    archive = os.path.join(outdir, name)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for relative in files:
                bundle.write(os.path.join(prefix, relative),
                             relative.replace(os.sep, "/"))
    else:
        with tarfile.open(archive, "w:gz") as bundle:
            for relative in files:
                bundle.add(os.path.join(prefix, relative), relative)
    with open(archive + ".sha256", "w", encoding="utf-8") as out:
        out.write(f"{_sha256(archive)}  {name}\n")

    print(f"Packaged Qt WebEngine {version} with the codecs:")
    print(f"  {archive}")
    print(f"  {archive}.sha256")
    print()
    print(f"Publish both as assets of a release tagged engine-{version} on")
    print("the Merlin repository. Every install of that engine version then")
    print("fetches it automatically. With the GitHub CLI:")
    print(f'  gh release create engine-{version} "{archive}" "{archive}.sha256" '
          f'--title "Qt WebEngine {version} with H.264 and AAC"')
    return 0




# ------------------------------------------------------------- inside Merlin
#
# Merlin cannot replace its engine while running it: Windows locks the loaded
# library. So an update is staged, downloaded and checked while Merlin runs,
# and applied at the next start, before the engine loads.
#
# A version that fails is remembered and not tried again, and "nothing is
# published for this version" is only rechecked once a day, so neither costs
# anything on an ordinary start.

def state_folder() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Merlin", "engine")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "merlin", "engine")


def _state_file() -> str:
    return os.path.join(state_folder(), "state.json")


def _read_state() -> dict:
    try:
        with open(_state_file(), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    os.makedirs(state_folder(), exist_ok=True)
    partial = _state_file() + ".part"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)
    os.replace(partial, _state_file())


def can_replace_engine() -> bool:
    """Whether this copy's engine is Merlin's to replace.

    Merlin.exe's own copy and a virtualenv's are. A distribution's Qt
    WebEngine belongs to the package manager and already has the codecs.
    """
    try:
        target = qt_folder()
    except Exception:                                    # noqa: BLE001
        return False
    if not os.path.isdir(target) or not os.access(target, os.W_OK):
        return False
    return not target.startswith(("/usr/lib", "/usr/lib64", "/usr/share"))


def should_stage(version: str) -> bool:
    """Whether looking for a build for this engine version is worth doing now."""
    import time

    if not version or not can_replace_engine():
        return False
    state = _read_state()
    if version in state.get("failed", []):
        return False
    if state.get("staged", {}).get("version") == version:
        return False
    checked = state.get("none_published", {})
    if checked.get("version") == version and time.time() - checked.get("at", 0) < 86400:
        return False
    return True


def stage(version: str, say=print) -> str:
    """Download and check the published build, ready for the next start.

    Returns "staged", "none" when nothing is published for this version yet,
    or "failed". Slow: call it off the UI thread.
    """
    import time

    cache = os.path.join(state_folder(), "cache")
    prefix, message = fetch_published(version, cache, say=say)
    state = _read_state()
    if not prefix:
        say(message)
        if "published yet" in message:
            state["none_published"] = {"version": version, "at": time.time()}
            _write_state(state)
            return "none"
        return "failed"
    offered = prefix_version(prefix)
    if offered and offered != version:
        say(f"The published build is {offered}, not {version}; not staging it.")
        return "failed"
    state["staged"] = {"version": version, "prefix": prefix}
    state.pop("none_published", None)
    _write_state(state)
    say(f"Qt WebEngine {version} with H.264 and AAC is ready for the next start.")
    return "staged"


def apply_staged(say=print) -> str:
    """Swap in a staged build, at start-up, before the engine has loaded.

    Checks it in a separate process and keeps it only if it plays H.264 and
    AAC. Returns "applied", "rolled back", or "" when there was nothing to do.
    """
    state = _read_state()
    staged = state.get("staged")
    if not staged:
        return ""
    if engine_in_use(qt_folder()):
        # kept staged, not marked failed: it is tried again next start
        say("Another Merlin window has the engine open; the update waits "
            "for the next start.")
        return ""
    version = staged.get("version", "")
    prefix = staged.get("prefix", "")
    state.pop("staged", None)
    # recorded before trying, so a crash part way through is not retried
    # on every start that follows
    state.setdefault("failed", [])
    if version not in state["failed"]:
        state["failed"].append(version)
    _write_state(state)

    if not (prefix and os.path.isdir(prefix) and can_replace_engine()):
        say("The staged engine is no longer usable; leaving the engine as it is.")
        return ""
    say(f"Applying the staged Qt WebEngine {version} with H.264 and AAC")
    target = qt_folder()
    # Staging only happens when this engine was found to lack the codecs, so
    # there is no need to start it once more to be told so: one check, of the
    # new engine, is all this start costs.
    known = {"engine": version, "h264": "", "aac": "", "mse_h264": False}
    kept = swap_in(prefix, target, check=probe, say=say, before=known)
    state = _read_state()
    if kept:
        state["failed"] = [v for v in state.get("failed", []) if v != version]
        state["applied"] = version
        _write_state(state)
        shutil.rmtree(prefix, ignore_errors=True)
        return "applied"
    return "rolled back"
