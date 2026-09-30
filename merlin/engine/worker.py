"""MerlinEngine's worker processes: parsing and styling pages in parallel.

Python runs one thread at a time (the global interpreter lock), so styling a
big page on a background thread still took turns with everything else in
Merlin, and slowed it all down while GitHub loaded. A separate process has an
interpreter of its own and truly runs alongside: this is how Chromium keeps
each tab's work apart too.

A worker is Merlin started again with --engine-worker: in Merlin.exe the same
executable, from source merlin-run.py. It loads no Qt, only the engine's pure
Python parts, and answers requests over its standard input and output, each a
length-prefixed pickle. It stays running, so each site's stylesheets are parsed
once and kept. If a worker cannot be started, or fails, the caller styles the
page itself, as before: slower, never broken.
"""
from __future__ import annotations

import os
import pickle
import struct
import subprocess
import sys
import threading
import traceback

WORKERS = 2                 # pages that can be styled at the same time
ANSWER_WITHIN = 60.0        # seconds before a worker is given up on


# ------------------------------------------------------------------ messages

def _send(stream, message) -> None:
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(struct.pack(">Q", len(data)))
    stream.write(data)
    stream.flush()


def _receive(stream):
    head = stream.read(8)
    if len(head) < 8:
        return None
    (size,) = struct.unpack(">Q", head)
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            return None
        data += chunk
    return pickle.loads(bytes(data))


# ------------------------------------------------------------------ the work

def style_page(job: dict) -> dict:
    """Parse the page and run the cascade: what a worker does for one request.

    job: markup, url, sheets (None for the page's own <style> blocks),
    viewport, hiding (the content blocker's CSS), scheme, and optionally
    patches ({element number: attributes}) for a page changed since loading,
    as when a <details> is opened. With want "document" the parsed page comes
    back with its styles; with "styles", only the styles in element order,
    for a page the caller already has.
    """
    from .css import Styler
    from .html import parse

    document = parse(job["markup"], job.get("url", ""))
    elements = [document.root] + list(document.root.elements())
    for number, attrs in (job.get("patches") or {}).items():
        if 0 <= number < len(elements):
            elements[number].attrs = dict(attrs)
    styler = Styler(document, author_css=job.get("sheets"),
                    viewport=tuple(job.get("viewport") or (1024, 768)),
                    extra_css=job.get("hiding", ""), scheme=job.get("scheme", "light"))
    styles = styler.compute()
    answer = {"media": dict(styler._media), "viewport_units": styler.viewport_units,
              "viewport": tuple(job.get("viewport") or (1024, 768))}
    if job.get("want") == "styles":
        answer["styles"] = [styles.get(element) for element in elements]
    else:
        answer["document"] = document
        answer["styles"] = styles
    return answer


def serve() -> int:
    """A worker's life: answer requests until Merlin closes the pipe."""
    sys.setrecursionlimit(20000)          # deep pages pickle deep
    inbox, outbox = sys.stdin.buffer, sys.stdout.buffer
    # nothing else may write to the channel the answers go on
    sys.stdout = sys.stderr
    while True:
        try:
            job = _receive(inbox)
        except Exception:                                  # noqa: BLE001
            return 1
        if job is None:
            return 0
        try:
            answer = style_page(job)
            answer["id"] = job.get("id")
        except Exception:                                  # noqa: BLE001
            answer = {"id": job.get("id"), "error": traceback.format_exc()}
        try:
            _send(outbox, answer)
        except Exception:                                  # noqa: BLE001
            return 1


# ------------------------------------------------------------------ Merlin's side

def worker_command() -> list:
    """How to start a worker for this copy of Merlin."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--engine-worker"]
    runner = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "merlin-run.py")
    return [sys.executable, runner, "--engine-worker"]


class _Worker:
    def __init__(self):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000           # no console window
        env = dict(os.environ, MERLIN_ENGINE_WORKER="1")
        self.process = subprocess.Popen(worker_command(), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=env, **kwargs)
        self.lock = threading.Lock()
        self.busy = 0

    def alive(self) -> bool:
        return self.process.poll() is None

    def ask(self, job: dict):
        with self.lock:                  # one request at a time per worker
            self.busy += 1
            try:
                _send(self.process.stdin, job)
                return _receive(self.process.stdout)
            finally:
                self.busy -= 1

    def stop(self) -> None:
        try:
            self.process.kill()
        except Exception:                                  # noqa: BLE001
            pass


class Pool:
    """Merlin's workers, started when first wanted and kept."""

    def __init__(self, size: int = WORKERS):
        self.size = size
        self.workers: list = []
        self.lock = threading.Lock()
        self.failed = False
        self.counter = 0

    def _pick(self):
        with self.lock:
            self.workers = [w for w in self.workers if w.alive()]
            idle = [w for w in self.workers if w.busy == 0]
            if idle:
                return idle[0]
            if len(self.workers) < self.size and not self.failed:
                try:
                    worker = _Worker()
                except Exception:                          # noqa: BLE001
                    self.failed = True           # cannot start here: style in-thread
                    return None
                self.workers.append(worker)
                return worker
            return min(self.workers, key=lambda w: w.busy) if self.workers else None

    def style(self, job: dict):
        """A worker's answer to job, or None: then the caller styles it itself.

        A worker that has died is found out on use (for a moment after dying it
        can still look alive), so a failed request is tried once more, with a
        worker started fresh.
        """
        if os.environ.get("MERLIN_ENGINE_WORKER") or self.failed:
            return None                         # a worker never starts workers
        answer, retry = self._style_once(job)
        if answer is None and retry and not self.failed:
            answer, _retry = self._style_once(job)
        return answer

    def _style_once(self, job: dict):
        """(answer or None, whether trying again is worth it)."""
        worker = self._pick()
        if worker is None:
            return None, False
        self.counter += 1
        job = dict(job, id=self.counter)
        result = {}

        def wait():
            try:
                result["answer"] = worker.ask(job)
            except Exception:                              # noqa: BLE001
                result["answer"] = None

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        waiter.join(ANSWER_WITHIN)
        answer = result.get("answer")
        if waiter.is_alive() or answer is None or "error" in answer:
            # stuck or broken: this worker goes, and is not picked again
            worker.stop()
            with self.lock:
                if worker in self.workers:
                    self.workers.remove(worker)
            # a dead worker fails at once, worth one more try; a stuck one has
            # already cost the full wait, and is not waited for twice
            return None, not waiter.is_alive()
        return answer, False

    def stop(self) -> None:
        for worker in self.workers:
            worker.stop()
        self.workers = []


POOL = Pool()
