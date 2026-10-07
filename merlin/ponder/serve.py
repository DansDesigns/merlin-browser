"""Ponder's server, started by Merlin (merlin/ponderhost.py).

    python serve.py --port 7000

Ponder's own code is unchanged beside this file and runs as it does on its
own (`python main.py`): its settings and index stay in ~/.config/ponder, so a
Ponder installed separately and this one share them. Differences:

  * it never opens a browser window: Merlin is the browser
  * it ends when Merlin does: Merlin holds this process's input open, and its
    end (Merlin closing, or Merlin killed outright) ends the server too, so
    no Ponder is left behind listening
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))


def _end_with_parent() -> None:
    """Exit once Merlin, holding this process's input, has gone."""
    def watch() -> None:
        try:
            while sys.stdin.buffer.read(1024):
                pass
        except Exception:                                  # noqa: BLE001
            pass
        os._exit(0)
    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--with-parent", action="store_true")
    args = parser.parse_args(argv)

    # Ponder's modules are top-level ones (config, main...): found here first
    sys.path.insert(0, HERE)
    os.chdir(HERE)
    if args.with_parent:
        _end_with_parent()

    import uvicorn

    import main as ponder                                  # noqa: E402  Ponder's app

    port = args.port or int(getattr(ponder.cfg, "port", 7000) or 7000)
    # where it really is, for what Ponder says of itself (its share address)
    ponder.cfg._data["port"] = port
    host = str(getattr(ponder.cfg, "host", "") or "127.0.0.1")
    uvicorn.run(ponder.app, host=host, port=port, log_level="warning",
                access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
