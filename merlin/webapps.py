"""Install a page as a standalone web app.

A web app is the current page plus a shortcut that opens it in its own frameless
window, with its own icon, separate from the browser. Nothing is downloaded or
bundled: the shortcut runs Merlin with `--app URL`, so the app always shows the
live site rather than a stale copy.

Shortcuts land in the Start Menu on Windows and in the applications directory on
Linux, so they appear wherever the system lists programs.
"""
from __future__ import annotations

import os
import shutil
import re
import subprocess
import sys

from . import settings as cfg
from .brand import APP_NAME

APPS_DIR = os.path.join(cfg.CONFIG_DIR, "webapps")


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")
    return (slug or "webapp")[:48]


def icon_dir() -> str:
    os.makedirs(APPS_DIR, exist_ok=True)
    return APPS_DIR


def save_icon(icon, slug: str) -> str:
    """Write the page's favicon out at the sizes the desktop needs."""
    if icon is None or icon.isNull():
        return ""
    target_png = os.path.join(icon_dir(), f"{slug}.png")
    pixmap = icon.pixmap(256, 256)
    if pixmap.isNull():
        pixmap = icon.pixmap(64, 64)
    if pixmap.isNull() or not pixmap.save(target_png, "PNG"):
        return ""

    if os.name == "nt":
        target_ico = os.path.join(icon_dir(), f"{slug}.ico")
        if _write_ico(icon, target_ico):
            return target_ico
    return target_png


def _write_ico(icon, path: str) -> bool:
    """Windows shortcuts need a real .ico, so build one from the favicon."""
    import struct

    from PyQt6.QtCore import QBuffer, QIODevice

    entries, blobs = b"", b""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        pixmap = icon.pixmap(size, size)
        if pixmap.isNull():
            continue
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            continue
        images.append((size, bytes(buffer.data())))
    if not images:
        return False

    offset = 6 + 16 * len(images)
    for size, data in images:
        width = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", width, width, 0, 0, 1, 32,
                               len(data), offset)
        blobs += data
        offset += len(data)
    try:
        with open(path, "wb") as handle:
            handle.write(struct.pack("<HHH", 0, 1, len(images)) + entries + blobs)
    except OSError:
        return False
    return True


def launcher_command() -> list[str]:
    """How to start Merlin, from wherever this copy happens to live.

    The installed launcher script is preferred over this interpreter and a
    script path: it keeps working if the virtualenv is rebuilt, which a
    reinstall does, whereas a baked-in path to a particular python does not.
    """
    if os.name != "nt":
        launcher = shutil.which(f"{cfg.APP_SLUG}-browser")
        if not launcher:
            candidate = os.path.expanduser(f"~/.local/bin/{cfg.APP_SLUG}-browser")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                launcher = candidate
        if launcher:
            return [launcher]

    run_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "merlin-run.py")
    if os.path.isfile(run_py):
        return [sys.executable, run_py]
    return [sys.executable, "-m", "merlin"]


def exec_value(parts: list) -> str:
    """Build a Desktop Entry Exec value.

    A percent sign starts a field code there, so a percent-encoded address, a
    search URL or anything with %20 in it, produces an invalid code such as
    "%2" and the launcher refuses to start the entry. It does so silently: the
    shortcut appears, and clicking it does nothing. Doubling the sign is what
    the specification asks for.
    """
    out = []
    for part in parts:
        escaped = part.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("%", "%%")
        out.append(f'"{escaped}"' if " " in part or any(
            ch in part for ch in "\t\n\"'\\><~|&;$*?#()`") else escaped)
    return " ".join(out)


def install(name: str, url: str, icon_path: str) -> tuple[bool, str]:
    if os.name == "nt":
        return _install_windows(name, url, icon_path)
    return _install_linux(name, url, icon_path)


def _install_linux(name: str, url: str, icon_path: str) -> tuple[bool, str]:
    apps = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "applications")
    os.makedirs(apps, exist_ok=True)
    slug = slugify(name)
    command = exec_value(launcher_command())
    desktop = os.path.join(apps, f"{cfg.APP_SLUG}-app-{slug}.desktop")
    body = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={name}, installed from {APP_NAME}\n"
        f"Exec={command} --app {exec_value([url])}\n"
        f"Icon={icon_path or 'merlin-browser'}\n"
        "Terminal=false\n"
        "Categories=Network;\n"
        f"StartupWMClass={cfg.APP_SLUG}-browser\n"
    )
    try:
        with open(desktop, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(desktop, 0o755)
    except OSError as exc:
        return False, f"Could not write the desktop entry: {exc}"
    # check=False covers a non-zero exit, not a missing executable: plenty of
    # systems have no update-desktop-database at all, and the entry works
    # without it.
    try:
        subprocess.run(["update-desktop-database", apps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    except (OSError, ValueError):
        pass
    return True, desktop


def _install_windows(name: str, url: str, icon_path: str) -> tuple[bool, str]:
    start_menu = os.path.join(os.environ.get("APPDATA", ""), "Microsoft",
                              "Windows", "Start Menu", "Programs")
    if not os.path.isdir(start_menu):
        return False, "Start Menu folder not found"
    link = os.path.join(start_menu, f"{slugify(name)}.lnk")

    command = launcher_command()
    target = command[0]
    arguments = " ".join(f'"{part}"' for part in command[1:])
    arguments = f'{arguments} --app "{url}"'.strip()

    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$link = $shell.CreateShortcut({_ps(link)})",
        f"$link.TargetPath = {_ps(target)}",
        f"$link.Arguments = {_ps(arguments)}",
        f"$link.WorkingDirectory = {_ps(os.path.dirname(target))}",
        f"$link.Description = {_ps(name)}",
        "$link.WindowStyle = 1",
    ] + ([f"$link.IconLocation = {_ps(icon_path)}"] if icon_path else []) + [
        "$link.Save()",
    ])

    ok, detail = run_powershell(script)
    if not ok:
        return False, detail
    if not os.path.isfile(link):
        return False, "PowerShell reported success but no shortcut appeared"
    return True, link


def run_powershell(script: str) -> tuple[bool, str]:
    """Run a PowerShell script passed as -EncodedCommand.

    The script used to be written to a .ps1 and passed with -File, which failed
    with "the argument ... does not exist" even though the folder was there.
    Encoding the script into the command line removes the temporary file, and
    with it the quoting rules and anything that might remove or lock the file
    between writing it and running it.
    """
    import base64
    import subprocess as sp

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = sp.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            stdout=sp.PIPE, stderr=sp.PIPE, check=False,
        )
    except OSError as exc:
        return False, f"Could not run PowerShell: {exc}"

    if result.returncode != 0:
        message = (result.stderr or result.stdout or b"")
        text = message.decode("utf-8", "replace").strip()
        return False, text[:400] or f"PowerShell exited with {result.returncode}"
    return True, ""


def _ps(value: str) -> str:
    """Single-quoted PowerShell literal, doubling any embedded quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def remove(entry: dict) -> bool:
    path = entry.get("shortcut", "")
    ok = True
    for candidate in (path, entry.get("icon", "")):
        if candidate and os.path.isfile(candidate):
            try:
                os.remove(candidate)
            except OSError:
                ok = False
    return ok
