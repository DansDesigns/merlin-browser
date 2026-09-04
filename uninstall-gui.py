#!/usr/bin/env python3
"""Merlin Browser, graphical uninstaller.

Like the installer, this does not do the work itself. It runs the uninstall
script that was written next to the installed copy and shows its output, so
there is one description of what removal means rather than two.

Tkinter, because it is the only toolkit guaranteed to be present, and because
the virtualenv containing PyQt6 is the thing being removed.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading

BG = "#1b1c20"
PANEL = "#232630"
TEXT = "#ececf0"
MUTED = "#9a9ba1"
DANGER = "#d9534f"


def install_root() -> str:
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                            "Merlin")
    return os.path.expanduser("~/.local/lib/merlin-browser")


def uninstall_script() -> str:
    root = install_root()
    name = "uninstall.bat" if os.name == "nt" else "uninstall.sh"
    candidate = os.path.join(root, name)
    if os.path.isfile(candidate):
        return candidate
    # running from the source folder, before anything was installed
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, name)
    return local if os.path.isfile(local) else ""


def _hidden() -> dict:
    """Keep the console window from flashing up on Windows."""
    if os.name != "nt":
        return {}
    settings = {"creationflags": 0x08000000}          # CREATE_NO_WINDOW
    try:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        settings["startupinfo"] = info
    except Exception:                                 # noqa: BLE001
        pass
    return settings


class Uninstaller:
    def __init__(self, root, tk, ttk):
        self.tk = tk
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.process = None
        self.finished = False
        self.failed = False

        root.title("Remove Merlin Browser")
        root.configure(bg=BG)
        root.geometry("680x520")
        root.minsize(600, 460)

        tk.Label(root, text="Remove Merlin Browser", bg=BG, fg=TEXT,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=24,
                                                     pady=(22, 4))
        tk.Label(root, bg=BG, fg=MUTED, justify="left", wraplength=610,
                 font=("Segoe UI", 10),
                 text="This removes the application, its shortcuts and the "
                      "virtualenv it runs in.").pack(anchor="w", padx=24)

        where = install_root()
        tk.Label(root, text=f"Installed at  {where}", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=24, pady=(10, 0))

        self.keep_profile = tk.BooleanVar(value=True)
        box = tk.Checkbutton(
            root, text="Keep my bookmarks, history and settings",
            variable=self.keep_profile, bg=BG, fg=TEXT, selectcolor=PANEL,
            activebackground=BG, activeforeground=TEXT, highlightthickness=0,
            bd=0, font=("Segoe UI", 10), anchor="w")
        box.pack(anchor="w", padx=22, pady=(14, 0))
        self.keep_box = box

        tk.Label(root, bg=BG, fg=MUTED, justify="left", wraplength=610,
                 font=("Segoe UI", 9),
                 text="Unticked, your profile is deleted as well and cannot be "
                      "recovered.").pack(anchor="w", padx=44)

        self.status = tk.Label(root, text="Ready", bg=BG, fg=TEXT,
                               font=("Segoe UI", 10))
        self.status.pack(anchor="w", padx=24, pady=(16, 6))

        self.log_frame = tk.Frame(root, bg=BG)
        self.log = tk.Text(self.log_frame, bg=PANEL, fg=MUTED, bd=0, wrap="none",
                           font=("Consolas" if os.name == "nt" else "monospace",
                                 9))
        scroll = tk.Scrollbar(self.log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log_frame.pack(fill="both", expand=True, padx=24)

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", side="bottom", padx=24, pady=18)
        self.close_button = tk.Button(
            buttons, text="Cancel", command=self.on_close, width=10, bg=PANEL,
            fg=TEXT, bd=0, activebackground="#2d303a", activeforeground=TEXT,
            font=("Segoe UI", 10), cursor="hand2")
        self.close_button.pack(side="right")
        self.action = tk.Button(
            buttons, text="Remove", command=self.start, width=14, bg=DANGER,
            fg="#ffffff", bd=0, activebackground="#e06b67",
            activeforeground="#ffffff", font=("Segoe UI", 10, "bold"),
            cursor="hand2")
        self.action.pack(side="right", padx=(0, 10))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self.drain)

        if not uninstall_script():
            self.status.configure(
                text="No installed copy was found, so there is nothing to do.")
            self.action.configure(state="disabled")

    def start(self) -> None:
        script = uninstall_script()
        if not script:
            return
        self.action.configure(state="disabled", text="Removing...")
        self.keep_box.configure(state="disabled")
        self.status.configure(text="Removing...")

        environment = dict(os.environ)
        environment["MERLIN_SILENT"] = "1"
        environment["MERLIN_KEEP_PROFILE"] = "1" if self.keep_profile.get() else "0"
        argv = (["cmd", "/c", script, "--yes"] if os.name == "nt"
                else ["bash", script])
        threading.Thread(target=self.run, args=(argv, environment),
                         daemon=True).start()

    def run(self, argv, environment) -> None:
        try:
            self.process = subprocess.Popen(
                argv, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                bufsize=1, errors="replace", **_hidden())
        except OSError as exc:
            self.queue.put(("failed", f"Could not start the uninstaller: {exc}"))
            return
        for line in self.process.stdout:
            self.queue.put(("line", line.rstrip("\n")))
        self.queue.put(("done", self.process.wait()))

    def drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[78]", "",
                                   payload).rstrip()
                    if clean.strip():
                        self.write(clean + "\n")
                        self.status.configure(text=clean.strip()[:70])
                elif kind == "failed":
                    self.on_done(1, payload)
                else:
                    self.on_done(payload, "")
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_done(self, code, message: str) -> None:
        self.finished = True
        self.failed = bool(code) or bool(message)
        if message:
            self.write(message + "\n")
        if self.failed:
            self.status.configure(text="Removal did not finish. The log says why.")
            self.action.configure(state="normal", text="Try again")
        else:
            kept = "Your profile was kept." if self.keep_profile.get() else ""
            self.status.configure(text=f"Merlin has been removed. {kept}".strip())
            self.action.configure(state="disabled", text="Removed")
        self.close_button.configure(text="Close")

    def on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.status.configure(
                text="Stopping now would leave Merlin half removed. Let it "
                     "finish.")
            return
        self.root.destroy()


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:                                    # noqa: BLE001
        script = uninstall_script() or "the uninstall script"
        print("Tkinter is not available, so the graphical uninstaller cannot "
              f"run.\nUse the text one instead:\n    {script}", file=sys.stderr)
        return 1

    root = tk.Tk()
    try:
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "merlin", "merlin.png")
        if os.path.isfile(icon):
            root.iconphoto(True, tk.PhotoImage(file=icon))
    except Exception:                                    # noqa: BLE001
        pass
    Uninstaller(root, tk, ttk)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
