#!/usr/bin/env python3
"""Rebuild Merlin's folder structure after a flattened download.

Downloading files one by one, or with a "download all" button, loses the
directory layout: every .py lands in a single folder and the merlin/ package
disappears. Python then cannot import merlin, and install.sh reports that it
cannot find merlin/app.py.

Run this in the folder holding the loose files:

    python3 setup-layout.py

It moves the twelve package modules into merlin/, restores executable bits,
normalises line endings (CRLF for .bat, LF for shell scripts), and then checks
that every module actually compiles. Safe to run twice.
"""
from __future__ import annotations

import os
import py_compile
import shutil
import sys
import tempfile

PACKAGE = "merlin"

# Modules that belong inside merlin/
MODULES = [
    "__init__.py", "__main__.py", "adblock.py", "app.py", "brand.py",
    "browser.py", "media.py", "playertab.py", "rustaudit.py", "settings.py",
    "store.py", "ui.py",
]

# Files that belong at the top level
TOP_LEVEL = [
    "install.sh", "install.bat", "uninstall.bat", "rename.sh",
    "requirements.txt", "README.md", "merlin-browser",
    "merlin-browser.desktop", "merlin-browser-frameless.desktop",
    "merlin.ico", "merlin.png",
]

EXECUTABLE = ["install.sh", "rename.sh", "merlin-browser"]
CRLF_FILES = ["install.bat", "uninstall.bat"]
LF_FILES = ["install.sh", "rename.sh", "merlin-browser",
            "merlin-browser.desktop", "merlin-browser-frameless.desktop"]

# Some browsers strip the extension. Match these by their opening bytes.
SIGNATURES = {
    "install.sh": b"# Merlin Browser - Linux installer",
    "rename.sh": b"# Rename the whole project",
    "install.bat": b"Merlin Browser - Windows installer",
    "uninstall.bat": b"Merlin Browser - Windows uninstaller",
    "merlin-browser": b"Merlin Browser launcher",
}

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = YELLOW = RED = RESET = ""


def ok(msg: str) -> None:
    print(f"  {GREEN}ok{RESET}    {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}note{RESET}  {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}miss{RESET}  {msg}")


def recover_extensionless() -> None:
    """Rename files whose extension the download stripped."""
    for target, signature in SIGNATURES.items():
        if os.path.exists(target):
            continue
        stem = target.rsplit(".", 1)[0]
        for candidate in (stem, stem + ".txt", target + ".txt"):
            if candidate == target or not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "rb") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            if signature in head:
                shutil.move(candidate, target)
                warn(f"renamed {candidate} -> {target}")
                break


def main() -> int:
    here = os.getcwd()
    print(f"\nRebuilding Merlin's layout in {here}\n")

    recover_extensionless()
    os.makedirs(PACKAGE, exist_ok=True)

    moved, present, missing = 0, 0, []
    for name in MODULES:
        inside = os.path.join(PACKAGE, name)
        if os.path.isfile(inside):
            present += 1
            continue
        if os.path.isfile(name):
            shutil.move(name, inside)
            moved += 1
            continue
        missing.append(name)

    if moved:
        ok(f"moved {moved} module(s) into {PACKAGE}/")
    if present:
        ok(f"{present} module(s) already in place")

    # __init__.py is the one file that can be regenerated safely
    init = os.path.join(PACKAGE, "__init__.py")
    if "__init__.py" in missing and not os.path.exists(init):
        with open(init, "w", encoding="utf-8") as fh:
            fh.write('"""Merlin Browser - a Chromium-engine browser shell '
                     'in Python + Qt/C++."""\n\n__version__ = "1.1.0"\n')
        missing.remove("__init__.py")
        warn("recreated merlin/__init__.py, which was missing")

    if "__main__.py" in missing:
        with open(os.path.join(PACKAGE, "__main__.py"), "w", encoding="utf-8") as fh:
            fh.write("import sys\n\nfrom .app import main\n\n"
                     'if __name__ == "__main__":\n'
                     "    raise SystemExit(main(sys.argv[1:]))\n")
        missing.remove("__main__.py")
        warn("recreated merlin/__main__.py, which was missing")

    for name in missing:
        bad(f"{PACKAGE}/{name} is not here; download it again")

    # top-level files
    absent_top = [n for n in TOP_LEVEL if not os.path.exists(n)]
    for name in absent_top:
        if name in ("merlin.ico", "merlin.png"):
            warn(f"{name} missing; only affects the icon")
        else:
            bad(f"{name} is missing")

    # line endings
    for name in CRLF_FILES:
        if os.path.isfile(name):
            raw = open(name, "rb").read()
            fixed = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if fixed != raw:
                open(name, "wb").write(fixed)
                ok(f"{name}: converted to CRLF (Windows needs this for goto)")
    for name in LF_FILES:
        if os.path.isfile(name):
            raw = open(name, "rb").read()
            fixed = raw.replace(b"\r\n", b"\n")
            if fixed != raw:
                open(name, "wb").write(fixed)
                ok(f"{name}: converted to LF")

    # executable bits
    if os.name != "nt":
        for name in EXECUTABLE:
            if os.path.isfile(name):
                mode = os.stat(name).st_mode
                os.chmod(name, mode | 0o111)
        ok("executable bits restored on scripts")

    # does it actually compile?
    print()
    broken = []
    for name in MODULES:
        path = os.path.join(PACKAGE, name)
        if not os.path.isfile(path):
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                py_compile.compile(path, cfile=os.path.join(tmp, "x.pyc"),
                                   doraise=True)
        except py_compile.PyCompileError as exc:
            broken.append((name, str(exc).splitlines()[-1]))

    if broken:
        for name, why in broken:
            bad(f"{PACKAGE}/{name} does not compile: {why}")
        print("\nA file is truncated or corrupted. Download that one again.\n")
        return 1

    if missing or [n for n in absent_top if n not in ("merlin.ico", "merlin.png")]:
        print("\nLayout fixed, but files are missing. Fill the gaps, then:\n")
    else:
        print(f"\n{GREEN}Layout is correct and every module compiles.{RESET}\n")

    print("  Linux    ./install.sh")
    print("  Windows  install.bat")
    print("  Or run without installing:  python3 -m merlin\n")
    return 0 if not (missing or broken) else 1


if __name__ == "__main__":
    sys.exit(main())
