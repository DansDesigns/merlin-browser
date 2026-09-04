#!/usr/bin/env python3
"""Merlin Browser, graphical installer.

Tkinter, because it is the only toolkit guaranteed to be there before anything
has been installed. PyQt6 is what this installs, so it cannot be what it is
written in.

It does not reimplement the install. It runs install.bat or install.sh
unattended and shows their output, so there is one description of how Merlin is
installed rather than two that drift apart. The step headings those scripts
print, "[3/7] ...", drive the progress bar.
"""
from __future__ import annotations

import os
import platform
import queue
import re
import subprocess
import sys
import threading

def _payload_dir() -> str:
    """Where the files to install are.

    Normally the folder this script sits in. When built into a single
    executable, PyInstaller unpacks the payload to a temporary folder and
    points sys._MEIPASS at it, and that is where install.bat and the merlin
    package will be.
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _payload_dir()


def _hidden() -> dict:
    """Keyword arguments that keep a console window from appearing.

    The installer scripts are console programs. Run from a windowed front end
    they would flash up their own black window, which is most of what the
    graphical installer exists to avoid.
    """
    if os.name != "nt":
        return {}
    settings = {"creationflags": 0x08000000}          # CREATE_NO_WINDOW
    try:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0                          # SW_HIDE
        settings["startupinfo"] = info
    except Exception:                                 # noqa: BLE001
        pass
    return settings
STEP = re.compile(r"^\s*\[(\d+)/(\d+)\]\s*(.+?)\s*$")

BG = "#1b1c20"
PANEL = "#232630"
TEXT = "#ececf0"
MUTED = "#9a9ba1"
ACCENT = "#6f8ff0"


def read_version() -> str:
    try:
        with open(os.path.join(HERE, "version.txt"), encoding="utf-8") as handle:
            return handle.readline().strip().split()[0]
    except (OSError, IndexError):
        return ""


class Installer:
    def __init__(self, root, tk, ttk):
        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.process = None
        self.finished = False
        self.failed = False

        root.title("Install Merlin Browser")
        root.configure(bg=BG)
        root.geometry("720x560")
        root.minsize(640, 480)

        version = read_version()
        heading = tk.Label(
            root, text="Merlin Browser" + (f"  {version}" if version else ""),
            bg=BG, fg=TEXT, font=("Segoe UI", 20, "bold"))
        heading.pack(anchor="w", padx=24, pady=(22, 2))

        tk.Label(root, bg=BG, fg=MUTED, justify="left", wraplength=640,
                 font=("Segoe UI", 10),
                 text="Merlin Browser: unleashing the magic of the internet. "
                      "Built on Python, C++, Qt and Chromium."
                 ).pack(anchor="w", padx=24)

        self.options = tk.Frame(root, bg=BG)
        self.options.pack(fill="x", padx=24, pady=(18, 6))

        self.desktop = tk.BooleanVar(value=True)
        self.system_qt = tk.BooleanVar(value=True)
        self.upgrade_pip = tk.BooleanVar(value=False)
        self._checkbox(self.options, "Add a desktop shortcut", self.desktop)
        self._checkbox(self.options,
                       "Replace pip in the new environment with the latest",
                       self.upgrade_pip)
        if os.name != "nt":
            self._checkbox(
                self.options,
                "Use the system Qt WebEngine (enables H.264 and AAC)",
                self.system_qt)

        where = (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                              "Merlin")
                 if os.name == "nt"
                 else os.path.expanduser("~/.local/lib/merlin-browser"))
        tk.Label(root, text=f"Installs to  {where}", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=24)

        self.status = tk.Label(root, text="Ready to install", bg=BG, fg=TEXT,
                               font=("Segoe UI", 10))
        self.status.pack(anchor="w", padx=24, pady=(16, 4))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:                                # noqa: BLE001
            pass
        style.configure("Merlin.Horizontal.TProgressbar",
                        troughcolor=PANEL, background=ACCENT,
                        bordercolor=PANEL, lightcolor=ACCENT,
                        darkcolor=ACCENT)
        self.bar = ttk.Progressbar(root, style="Merlin.Horizontal.TProgressbar",
                                   maximum=100, length=100)
        self.bar.pack(fill="x", padx=24)

        self.detail_shown = tk.BooleanVar(value=False)
        toggle = tk.Button(root, text="Show details", command=self.toggle_detail,
                           bg=BG, fg=MUTED, bd=0, highlightthickness=0,
                           activebackground=BG, activeforeground=TEXT,
                           font=("Segoe UI", 9), cursor="hand2")
        toggle.pack(anchor="w", padx=20, pady=(8, 0))
        self.toggle = toggle

        self.log_frame = tk.Frame(root, bg=BG)
        self.log = tk.Text(self.log_frame, bg=PANEL, fg=MUTED, bd=0,
                           insertbackground=TEXT, wrap="none",
                           font=("Consolas" if os.name == "nt" else "monospace",
                                 9))
        scroll = tk.Scrollbar(self.log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill="x", side="bottom", padx=24, pady=18)
        self.close_button = tk.Button(
            buttons, text="Cancel", command=self.on_close, width=10,
            bg=PANEL, fg=TEXT, bd=0, activebackground="#2d303a",
            activeforeground=TEXT, font=("Segoe UI", 10), cursor="hand2")
        self.close_button.pack(side="right")
        self.action = tk.Button(
            buttons, text="Install", command=self.start, width=14,
            bg=ACCENT, fg="#10121a", bd=0, activebackground="#8aa4f5",
            activeforeground="#10121a", font=("Segoe UI", 10, "bold"),
            cursor="hand2")
        self.action.pack(side="right", padx=(0, 10))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self.drain)

    def _checkbox(self, parent, text, variable):
        box = self.tk.Checkbutton(
            parent, text=text, variable=variable, bg=BG, fg=TEXT,
            selectcolor=PANEL, activebackground=BG, activeforeground=TEXT,
            highlightthickness=0, bd=0, font=("Segoe UI", 10),
            anchor="w")
        box.pack(anchor="w", pady=2)
        return box

    def toggle_detail(self) -> None:
        if self.detail_shown.get():
            self.log_frame.pack_forget()
            self.toggle.configure(text="Show details")
            self.detail_shown.set(False)
        else:
            self.log_frame.pack(fill="both", expand=True, padx=24, pady=(6, 0))
            self.toggle.configure(text="Hide details")
            self.detail_shown.set(True)

    # ------------------------------------------------------------- running
    def command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd", "/c", os.path.join(HERE, "install.bat"), "--yes"]
        script = os.path.join(HERE, "install.sh")
        argv = ["bash", script, "--yes"]
        argv.append("--system-qt" if self.system_qt.get() else "--venv-only")
        return argv

    def start(self) -> None:
        missing = self.missing_pieces()
        if missing:
            self.write(missing + "\n")
            self.status.configure(text=missing)
            if not self.detail_shown.get():
                self.toggle_detail()
            return

        self.action.configure(state="disabled", text="Installing...")
        for child in self.options.winfo_children():
            child.configure(state="disabled")
        self.status.configure(text="Starting...")
        self.bar.configure(value=2)

        environment = dict(os.environ)
        environment["MERLIN_SILENT"] = "1"
        environment["MERLIN_DESKTOP"] = "1" if self.desktop.get() else "0"
        environment["MERLIN_ASSUME_YES"] = "1"
        environment["MERLIN_UPGRADE_PIP"] = "1" if self.upgrade_pip.get() else "0"
        threading.Thread(target=self.run, args=(environment,),
                         daemon=True).start()

    def missing_pieces(self) -> str:
        script = "install.bat" if os.name == "nt" else "install.sh"
        if not os.path.isfile(os.path.join(HERE, script)):
            return (f"{script} is not next to this program. Run the installer "
                    "from the folder you extracted.")
        if not os.path.isdir(os.path.join(HERE, "merlin")):
            return "The merlin folder is missing from this download."
        return ""

    def run(self, environment) -> None:
        try:
            self.process = subprocess.Popen(
                self.command(), cwd=HERE, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                errors="replace", **_hidden())
        except OSError as exc:
            self.queue.put(("failed", f"Could not start the installer: {exc}"))
            return

        for line in self.process.stdout:
            self.queue.put(("line", line.rstrip("\n")))
        code = self.process.wait()
        self.queue.put(("done", code))

    # ------------------------------------------------------------ updating
    def drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    self.on_line(payload)
                elif kind == "failed":
                    self.on_done(1, payload)
                else:
                    self.on_done(payload, "")
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def on_line(self, line: str) -> None:
        # the installers draw their own bar with control codes; strip them
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[78]", "", line).rstrip()
        if clean.strip():
            self.write(clean + "\n")
        match = STEP.match(clean)
        if match:
            done, total, label = match.groups()
            self.bar.configure(value=int(done) / int(total) * 100)
            self.status.configure(text=label)

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
            self.bar.configure(value=100)
            self.status.configure(
                text="Installation did not finish. The details say why.")
            self.action.configure(state="normal", text="Try again")
            if not self.detail_shown.get():
                self.toggle_detail()
        else:
            self.bar.configure(value=100)
            self.status.configure(text="Merlin is installed.")
            self.action.configure(state="normal", text="Launch Merlin",
                                  command=self.launch)
        self.close_button.configure(text="Close")

    def launch(self) -> None:
        try:
            if os.name == "nt":
                root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                    "Programs", "Merlin")
                built = os.path.join(root, "bin", "Merlin", "Merlin.exe")
                # prefer the built executable, so no console is involved at all
                if os.path.isfile(built):
                    subprocess.Popen([built], **_hidden())
                else:
                    subprocess.Popen(
                        ["cmd", "/c", os.path.join(root, "merlin-browser.cmd")],
                        **_hidden())
            else:
                subprocess.Popen(
                    [os.path.expanduser("~/.local/bin/merlin-browser")],
                    start_new_session=True)
        except OSError as exc:
            self.write(f"Could not start Merlin: {exc}\n")
            return
        self.root.destroy()

    def on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.status.configure(
                text="Stopping would leave a half-finished install. "
                     "Let it run.")
            return
        self.root.destroy()


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:                                    # noqa: BLE001
        script = "install.bat" if os.name == "nt" else "./install.sh"
        print("Tkinter is not available, so the graphical installer cannot "
              "run.\n"
              + ("Install python3-tk from your package manager, "
                 if os.name != "nt" else "")
              + f"or use the text installer instead:\n    {script}",
              file=sys.stderr)
        return 1

    root = tk.Tk()
    try:
        icon = os.path.join(HERE, "merlin", "merlin.png")
        if os.path.isfile(icon):
            root.iconphoto(True, tk.PhotoImage(file=icon))
    except Exception:                                    # noqa: BLE001
        pass
    Installer(root, tk, ttk)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
