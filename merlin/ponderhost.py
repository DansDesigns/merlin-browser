"""Ponder, built into Merlin as its search engine.

Ponder's code is in merlin/ponder, unchanged but for one fallback, and runs as
its own process (merlin/ponder/serve.py), as it does on its own: its modules
are top-level ones, and a server's work stays off Merlin's own process. Its
settings and index are Ponder's own (~/.config/ponder), so a Ponder installed
separately and this one are the same Ponder to its user.

  * A Ponder already answering on its port (one started separately) is used
    as it is, and left alone when Merlin closes.
  * Otherwise Merlin starts its own, on Ponder's port, or a free one if
    something else holds that, and ends it when Merlin closes (and if Merlin
    is killed: the server ends when its input from Merlin does).
  * Its parts (FastAPI and the rest, none of them Rust) are installed by the
    installers; a Merlin updated from before Ponder was built in installs them
    itself, once, into its own environment.
  * Started in the background soon after Merlin opens, when Ponder is the
    search engine, so it is ready by the first search; a search made sooner
    waits for it (off the window's thread).
  * If it cannot be started, searches go to DuckDuckGo and Settings says why.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

PONDER_DEFAULT_PORT = 7000
STATUS_PATH = "/api/status"
START_WAIT = 60.0               # seconds for a started server to answer
INSTALL_WAIT = 600.0            # pip, the first time, on a slow line
NEEDED = ("fastapi", "uvicorn", "httpx", "bs4", "lxml", "whoosh", "multipart")

_lock = threading.Lock()
_ready = threading.Event()
_state = {"phase": "idle", "port": 0, "ours": False, "reason": "", "process": None,
          "thread": None, "failed_at": 0.0}
RETRY_AFTER = 300.0             # a failed start is tried again after this long


# ------------------------------------------------------------------ places
def ponder_dir() -> str:
    """The folder holding Ponder's code (serve.py and main.py)."""
    here = os.path.dirname(os.path.abspath(__file__))
    places = [os.path.join(here, "ponder")]
    if getattr(sys, "frozen", False):
        exe = os.path.dirname(os.path.abspath(sys.executable))
        places.append(os.path.abspath(os.path.join(exe, "..", "..", "app", "merlin", "ponder")))
    for place in places:
        if os.path.isfile(os.path.join(place, "serve.py")):
            return place
    return places[0]


def python_for_ponder() -> str:
    """An interpreter that can run Ponder: Merlin's own, or, for Merlin.exe
    (which holds only what Merlin itself imports), the environment beside it."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    exe = os.path.dirname(os.path.abspath(sys.executable))
    app = os.path.dirname(os.path.dirname(ponder_dir()))
    for venv in (os.path.join(exe, "..", "..", "venv"), os.path.join(app, "..", "venv")):
        for name in (("Scripts", "pythonw.exe"), ("Scripts", "python.exe"), ("bin", "python3"),
                     ("bin", "python")):
            candidate = os.path.abspath(os.path.join(venv, *name))
            if os.path.isfile(candidate):
                return candidate
    return ""


def ponder_port() -> int:
    """The port Ponder's own settings give (7000 unless changed there)."""
    path = os.path.join(os.path.expanduser("~"), ".config", "ponder", "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            port = int(json.load(handle).get("port") or PONDER_DEFAULT_PORT)
        return port if 0 < port < 65536 else PONDER_DEFAULT_PORT
    except (OSError, ValueError, TypeError, AttributeError):
        return PONDER_DEFAULT_PORT


def _log_path() -> str:
    try:
        from . import settings as cfg

        folder = cfg.CACHE_DIR
    except Exception:                                      # noqa: BLE001
        import tempfile

        folder = tempfile.gettempdir()
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "ponder-log.txt")


def _note(text: str) -> None:
    try:
        from . import crashlog

        crashlog.note("ponder: " + text)
    except Exception:                                      # noqa: BLE001
        pass


# ------------------------------------------------------------------ checks
def answers(port: int, timeout: float = 1.0) -> bool:
    """Whether a Ponder is answering on port (not just anything listening)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{STATUS_PATH}", timeout=timeout) as response:
            found = json.loads(response.read(65536) or b"{}")
        return isinstance(found, dict) and "doc_count" in found
    except Exception:                                      # noqa: BLE001
        return False


def _port_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _quiet() -> dict:
    """No console window for a helper on Windows."""
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def parts_present(python: str) -> bool:
    try:
        done = subprocess.run([python, "-c", "import " + ", ".join(NEEDED)], capture_output=True,
                              timeout=60, stdin=subprocess.DEVNULL, **_quiet())
        return done.returncode == 0
    except Exception:                                      # noqa: BLE001
        return False


def _in_own_environment(python: str) -> bool:
    """Whether pip may install into python's packages: a venv (Merlin's own),
    never a system Python that the distribution manages."""
    if getattr(sys, "frozen", False):
        return True                                        # the venv beside Merlin.exe
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def install_parts(python: str) -> str:
    """pip install Ponder's requirements; "" when done, else why not."""
    requirements = os.path.join(ponder_dir(), "requirements.txt")
    if not _in_own_environment(python):
        return ("Ponder's parts are not installed, and this Python is the system's: "
                f"install them with  pip install -r \"{requirements}\"")
    try:
        with open(_log_path(), "a", encoding="utf-8") as log:
            log.write(f"\n--- installing Ponder's parts {time.ctime()}\n")
            log.flush()
            done = subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check",
                                   "-r", requirements], stdout=log, stderr=log,
                                  stdin=subprocess.DEVNULL, timeout=INSTALL_WAIT, **_quiet())
    except Exception as exc:                               # noqa: BLE001
        return f"Ponder's parts could not be installed: {exc}"
    if done.returncode != 0:
        return "Ponder's parts could not be installed (see ponder-log.txt)"
    return ""


# ------------------------------------------------------------------ running
def _set(phase: str, reason: str = "") -> None:
    with _lock:
        _state["phase"] = phase
        _state["reason"] = reason
        if phase == "failed":
            _state["failed_at"] = time.monotonic()
    if phase in ("ready", "failed"):
        _ready.set()
    _note(phase + (f": {reason}" if reason else ""))


def _bring_up() -> None:
    port = ponder_port()
    with _lock:
        _state["port"] = port
    if answers(port):
        with _lock:
            _state["ours"] = False
        _set("ready", f"using the Ponder already running on port {port}")
        return
    python = python_for_ponder()
    serve = os.path.join(ponder_dir(), "serve.py")
    if not python or not os.path.isfile(serve):
        _set("failed", "Ponder is missing from this copy of Merlin")
        return
    if not parts_present(python):
        _set("installing")
        problem = install_parts(python)
        if problem or not parts_present(python):
            _set("failed", problem or "Ponder's parts would not load after installing")
            return
    if not _port_free(port):
        port = _free_port()
        with _lock:
            _state["port"] = port
    _set("starting")
    try:
        log = open(_log_path(), "a", encoding="utf-8")
        log.write(f"\n--- Ponder starting on port {port} {time.ctime()}\n")
        log.flush()
        process = subprocess.Popen([python, serve, "--port", str(port), "--with-parent"],
                                   stdin=subprocess.PIPE, stdout=log, stderr=log,
                                   cwd=ponder_dir(), **_quiet())
        log.close()
    except Exception as exc:                               # noqa: BLE001
        _set("failed", f"Ponder would not start: {exc}")
        return
    with _lock:
        _state["process"] = process
        _state["ours"] = True
    give_up = time.monotonic() + START_WAIT
    while time.monotonic() < give_up:
        if process.poll() is not None:
            _set("failed", "Ponder stopped as it started (see ponder-log.txt)")
            return
        if answers(port, timeout=0.5):
            _set("ready", f"started on port {port}")
            return
        time.sleep(0.25)
    _set("failed", "Ponder did not answer in time (see ponder-log.txt)")


def ensure() -> None:
    """Have Ponder running, or on its way: returns at once."""
    if os.environ.get("MERLIN_NO_PONDER"):                # tests, and anyone who wants none
        with _lock:
            if _state["phase"] != "failed":
                _state.update(phase="failed", reason="turned off (MERLIN_NO_PONDER)",
                              failed_at=time.monotonic())
            _ready.set()
        return
    with _lock:
        if _state["phase"] in ("checking", "installing", "starting", "ready"):
            return
        if _state["phase"] == "failed" and time.monotonic() - _state["failed_at"] < RETRY_AFTER:
            return                                         # not at every search
        _ready.clear()
        _state["phase"] = "checking"
        worker = threading.Thread(target=_bring_up, daemon=True, name="ponder-start")
        _state["thread"] = worker
    worker.start()


def wait_ready(timeout: float = START_WAIT) -> bool:
    """Wait (never on the window's thread) for Ponder; whether it answers."""
    ensure()
    with _lock:
        installing = _state["phase"] == "installing"
    _ready.wait(INSTALL_WAIT if installing else timeout)
    with _lock:
        return _state["phase"] == "ready"


def phase() -> str:
    with _lock:
        return _state["phase"]


def reason() -> str:
    with _lock:
        return _state["reason"]


def failed() -> bool:
    return phase() == "failed"


def port() -> int:
    with _lock:
        return _state["port"] or ponder_port()


def is_ponder_url(url: str) -> bool:
    """Whether url is this Ponder's own address (searches, its pages)."""
    import urllib.parse

    parts = urllib.parse.urlsplit(url or "")
    return (parts.scheme == "http" and (parts.hostname or "") in ("localhost", "127.0.0.1")
            and (parts.port or 80) == port())


def wait_if_starting(url: str) -> None:
    """Before fetching url: if it is Ponder's and Ponder is not up yet, wait."""
    if is_ponder_url(url) and phase() != "ready":
        wait_ready()


def describe() -> str:
    """One line for Settings."""
    current, why = phase(), reason()
    return {
        "idle": "Ponder starts when it is first needed.",
        "checking": "Ponder is starting...",
        "installing": "Installing Ponder's parts (once)...",
        "starting": "Ponder is starting...",
        "ready": f"Ponder is running: {why}.",
        "failed": f"Ponder could not start, so searches go to DuckDuckGo: {why}.",
    }.get(current, current)


def stop() -> None:
    """Merlin is closing: end the Ponder it started (one it found, it leaves)."""
    with _lock:
        process = _state["process"] if _state["ours"] else None
        _state["process"] = None
        _state["phase"] = "idle"
        _ready.clear()
    if process is None or process.poll() is not None:
        return
    try:
        process.stdin.close()                              # serve.py ends on this
    except Exception:                                      # noqa: BLE001
        pass
    try:
        process.wait(timeout=2)
    except Exception:                                      # noqa: BLE001
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:                                  # noqa: BLE001
            try:
                process.kill()
            except Exception:                              # noqa: BLE001
                pass
