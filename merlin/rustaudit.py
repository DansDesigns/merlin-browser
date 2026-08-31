"""Runtime Rust audit.

The claim "this browser contains no Rust" is only worth anything if it can be
checked on the machine that is actually running it, against the libraries that
are actually loaded. This module walks the process tree (Chromium spawns zygote,
GPU, network, utility and renderer processes), collects every mapped shared
object, and scans each one for compiler fingerprints that Rust leaves in release
binaries.

Detection markers, in decreasing order of reliability:
  /rustc/<hash>/library/...   remapped std source paths baked into panic sites
  library/core/src/...        the same, when only partially remapped
  attempt to add with overflow, called `Option::unwrap()`...  panic strings
  _ZN...17h<16 hex>E          legacy Rust symbol mangling

A stripped Rust binary still carries panic-site strings in .rodata, because the
panic machinery needs them at runtime. Verified against a known-Rust library
(python-cryptography's _rust.abi3.so) as a positive control: 55 hits for
"/rustc/" versus 0 in libQt6WebEngineCore.

Linux and macOS read /proc and vmmap. Windows walks the module list of each
process with Toolhelp32, which needs no privileges for your own processes.

Run standalone:   merlin-browser --audit-rust
"""
from __future__ import annotations

import os
import re
import sys

CHUNK = 8 * 1024 * 1024
OVERLAP = 128

MARKERS: list[tuple[bytes, str, int]] = [
    (b"/rustc/", "remapped rustc std path", 3),
    (b"library/core/src", "Rust core source path", 3),
    (b"attempt to add with overflow", "Rust overflow panic string", 2),
    (b"called `Option::unwrap()` on a `None` value", "Rust unwrap panic string", 2),
    (b"called `Result::unwrap()` on an `Err` value", "Rust unwrap panic string", 2),
    (b"cargo/registry/src", "cargo dependency path", 3),
]

MANGLE_RE = re.compile(rb"17h[0-9a-f]{16}E")

# Libraries that commonly do contain Rust on a Linux desktop, so the report can
# tell the user what to do about a hit instead of just flagging it.
KNOWN_RUST_HINTS = {
    "librav1e": "Rust AV1 encoder. Not used for playback. Ubuntu's "
                "libavcodec.so has a DT_NEEDED entry for it, so anything loading "
                "FFmpeg in-process gets it: in Merlin that means the libvlc "
                "player mode. Switch Settings, Media to a process-separated mode, "
                "or rebuild FFmpeg with --disable-librav1e.",
    "librsvg": "Rust SVG renderer. Two possible causes: FFmpeg links it "
               "(so the libvlc player mode loads it, fix as for librav1e), or a "
               "GTK platform theme pulled in gdk-pixbuf (set "
               "QT_QPA_PLATFORMTHEME=qt6ct).",
    "libgstrs": "gst-plugins-rs. Uninstall it; gst-plugins-good/bad/ugly are C.",
    "gstreamer-1.0/libgst": "A GStreamer plugin. Check whether it comes from "
                            "gst-plugins-rs.",
    "libvulkan_nouveau": "Mesa NVK. Its shader compiler (NAK) is Rust. Use the "
                         "proprietary NVIDIA driver or force GL instead of Vulkan.",
    "rusticl": "Mesa's OpenCL implementation, written in Rust. Not needed by the "
               "browser; disable it with RUSTICL_ENABLE unset.",
    "_rust": "A Python extension module built from Rust.",
    "libssh": "Some builds link Rust TLS backends. Check your distro build.",
    "avcodec": "FFmpeg. On Windows this arrives only with an in-process player "
               "backend; switch Settings, Media to a process-separated mode.",
    "libSvtAv1": "AV1 encoder pulled in by FFmpeg. Same fix as librav1e.",
}


# --------------------------------------------------------------- Windows
# Toolhelp32 structures, declared with plain ctypes types so this module still
# imports on Linux. Windows support is written to the documented API but has not
# been executed on Windows by its author; every call is guarded, and a failure
# reports itself rather than raising.
if os.name == "nt":
    import ctypes

    TH32CS_SNAPPROCESS = 0x00000002
    TH32CS_SNAPMODULE = 0x00000008
    TH32CS_SNAPMODULE32 = 0x00000010
    MAX_PATH = 260
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * MAX_PATH),
        ]

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("th32ModuleID", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("GlblcntUsage", ctypes.c_ulong),
            ("ProccntUsage", ctypes.c_ulong),
            ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
            ("modBaseSize", ctypes.c_ulong),
            ("hModule", ctypes.c_void_p),
            ("szModule", ctypes.c_wchar * 256),
            ("szExePath", ctypes.c_wchar * MAX_PATH),
        ]


def _windows_process_pairs() -> list[tuple[int, int]]:
    """Every (pid, parent pid) on the system."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE:
        return []
    pairs = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pairs


def _windows_modules(pid: int) -> list[str]:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot == INVALID_HANDLE:
        return []
    paths = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExePath:
                paths.append(entry.szExePath)
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return paths


# ----------------------------------------------------------------- generic
def process_tree(root_pid: int | None = None) -> list[int]:
    """Return root_pid and every descendant process id."""
    root_pid = root_pid or os.getpid()
    children: dict[int, list[int]] = {}

    if os.name == "nt":
        try:
            for pid, ppid in _windows_process_pairs():
                children.setdefault(ppid, []).append(pid)
        except Exception:                                # noqa: BLE001
            return [root_pid]
    else:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return [root_pid]
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    data = fh.read()
                ppid = int(data[data.rfind(b")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(pid)

    seen: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.append(pid)
        stack.extend(children.get(pid, []))
    return seen


def mapped_libraries(pids: list[int]) -> list[str]:
    libs: set[str] = set()

    if os.name == "nt":
        for pid in pids:
            try:
                for path in _windows_modules(pid):
                    if path.lower().endswith((".dll", ".pyd", ".exe")):
                        libs.add(path)
            except Exception:                            # noqa: BLE001
                continue
        return sorted(libs)

    for pid in pids:
        try:
            with open(f"/proc/{pid}/maps", "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 6:
                        continue
                    path = parts[-1]
                    if path.startswith("/") and (".so" in path or path.endswith(".node")):
                        libs.add(path)
        except OSError:
            continue
    return sorted(libs)


def scan_file(path: str) -> list[str]:
    """Return the list of Rust markers found in one file."""
    found: list[str] = []
    try:
        size = os.path.getsize(path)
    except OSError:
        return found
    if size == 0:
        return found
    try:
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                for needle, label, _weight in MARKERS:
                    if label not in found and needle in window:
                        found.append(label)
                if "Rust symbol mangling" not in found and MANGLE_RE.search(window):
                    found.append("Rust symbol mangling")
                if len(found) >= 3:
                    break
                tail = window[-OVERLAP:]
    except OSError:
        return found
    return found


def hint_for(path: str) -> str:
    lowered = path.lower()
    for fragment, advice in KNOWN_RUST_HINTS.items():
        if fragment.lower() in lowered:
            return advice
    return "Unexpected. Report the path so the dependency can be traced."


def audit(root_pid: int | None = None) -> dict:
    pids = process_tree(root_pid)
    libs = mapped_libraries(pids)
    findings = []
    for lib in libs:
        markers = scan_file(lib)
        if markers:
            findings.append({"path": lib, "markers": markers, "hint": hint_for(lib)})
    return {
        "platform": f"{sys.platform} ({os.name})",
        "processes": len(pids),
        "libraries": len(libs),
        "findings": findings,
        "clean": not findings,
        "library_list": libs,
    }


def format_report(result: dict, verbose: bool = False) -> str:
    lines = [
        "Rust audit",
        "=" * 60,
        f"Platform          : {result.get('platform', '?')}",
        f"Processes scanned : {result['processes']}",
        f"Libraries scanned : {result['libraries']}",
        "",
    ]
    if result["libraries"] == 0:
        lines.append("WARNING: no libraries were readable, so this result proves "
                     "nothing. Report it as a bug.")
        lines.append("")
    if result["clean"]:
        lines.append("RESULT: no Rust detected in any loaded library.")
    else:
        lines.append(f"RESULT: Rust found in {len(result['findings'])} library/libraries.")
        lines.append("")
        for finding in result["findings"]:
            lines.append(f"  {finding['path']}")
            lines.append(f"    markers: {', '.join(finding['markers'])}")
            lines.append(f"    {finding['hint']}")
            lines.append("")
    if verbose:
        lines.append("")
        lines.append("Libraries scanned:")
        lines.extend("  " + lib for lib in result["library_list"])
    return "\n".join(lines)


def main_standalone() -> int:
    """Load the engine, then audit the running process tree."""
    os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", ""))
    from PyQt6.QtWidgets import QApplication
    from PyQt6 import QtWebEngineWidgets  # noqa: F401
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtCore import QTimer

    app = QApplication([sys.argv[0]])
    view = QWebEngineView()
    view.setHtml("<video></video><canvas></canvas>")
    view.resize(320, 240)
    view.show()

    code = {"value": 0}

    def run():
        result = audit()
        print(format_report(result, verbose="-v" in sys.argv))
        code["value"] = 0 if result["clean"] else 2
        app.quit()

    # give Chromium time to spawn its helper processes and load GPU/media libs
    QTimer.singleShot(4000, run)
    app.exec()
    return code["value"]
