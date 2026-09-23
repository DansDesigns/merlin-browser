#!/usr/bin/env python3
"""Put a Qt WebEngine built with H.264 and AAC into Merlin, and prove it works.

    python tools/use-codec-engine.py --auto            what the installers run
    python tools/use-codec-engine.py <prefix>          install a local build
    python tools/use-codec-engine.py --package <prefix> <outdir>
                                                       make a release asset
    python tools/use-codec-engine.py --restore         put the original back
    python tools/use-codec-engine.py --check           report what is in use

--auto is how users get it, with nothing to do by hand. It fetches the build
of exactly the Qt WebEngine version just installed, published on the Merlin
repository by .github/workflows/build-webengine-codecs.yml, checks it against
its published SHA-256, swaps it in, and keeps it only if it really plays H.264
and AAC. If nothing is published for this version yet it says so and leaves the
standard engine; that is not an error. MERLIN_WEBENGINE_CODECS can name a local
build to use instead.

The work itself is in merlin/codecengine.py, shared with Merlin's own updater,
so there is one implementation of the download checks and the rollback. Run
this with the Python from Merlin's virtualenv, so it finds that PyQt6.

Exit codes for --auto: 0 codecs in place, 2 none available, 1 failed.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from merlin.codecengine import (                          # noqa: E402
    asset_name, asset_url, describe, engine_files, fetch_published, has_codecs,
    package, prefix_version, qt_folder, restore, swap_in,
)
from merlin.codecengine import probe as _probe            # noqa: E402

__all__ = ["asset_name", "asset_url", "describe", "engine_files",
           "fetch_published", "has_codecs", "package", "prefix_version",
           "qt_folder", "restore", "swap_in"]


def say(text: str) -> None:
    print("      " + text, flush=True)


def check() -> dict:
    """The engine in this virtualenv, started in a child Python."""
    return _probe(python=sys.executable)


def auto(remembered: str, cache: str) -> int:
    before = check()
    say("Engine: " + describe(before))
    if has_codecs(before):
        say("It already plays H.264 and AAC.")
        return 0
    version = before.get("engine", "")
    if not version:
        say("Could not read the engine's version, so leaving it as it is.")
        return 1

    local = os.environ.get("MERLIN_WEBENGINE_CODECS", "")
    if not local and remembered and os.path.isfile(remembered):
        local = open(remembered, encoding="utf-8").read().strip()
    if local and os.path.isdir(local) and prefix_version(local) in ("", version):
        say(f"Using the local build in {local}")
        if swap_in(local, qt_folder(), check, say=say, before=before):
            return 0

    prefix, message = fetch_published(version, cache, say=say)
    if not prefix:
        say(message)
        say("The standard engine stays: YouTube live will offer Merlin's "
            "player instead.")
        return 2
    return 0 if swap_in(prefix, qt_folder(), check, say=say, before=before) else 1


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--check":
        print(describe(check()))
        return 0
    if argv[0] == "--restore":
        return 0 if restore(qt_folder()) else 1
    if argv[0] == "--auto":
        remembered = argv[1] if len(argv) > 1 else ""
        cache = argv[2] if len(argv) > 2 else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))),
            "engine-cache")
        return auto(remembered, cache)
    if argv[0] == "--package":
        if len(argv) < 3:
            print("usage: use-codec-engine.py --package <prefix> <outdir>")
            return 1
        return package(os.path.abspath(argv[1]), os.path.abspath(argv[2]))
    return 0 if swap_in(os.path.abspath(argv[0]), qt_folder(), check, say=say) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
