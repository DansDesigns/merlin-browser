#!/usr/bin/env python3
"""Exercise the code paths that only run on Windows.

A NameError inside an `if os.name == "nt"` block shipped once: QTimer was used
a few lines above its import, and every test skipped the branch. This forces
those paths to execute on any platform by pretending the Windows API is present
but inert, so a missing name or a bad call fails here rather than on a user's
machine.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu --disable-dev-shm-usage")

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  {status}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)


def fake_windows_api():
    """A stand-in for ctypes.windll that records calls instead of making them."""
    calls = []

    class FakeFunc:
        def __init__(self, name, result=1):
            self.name, self.result = name, result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    class FakeLib:
        def __init__(self, name):
            self._name = name
            self._funcs = {}

        def __getattr__(self, item):
            if item not in self._funcs:
                self._funcs[item] = FakeFunc(item)
            return self._funcs[item]

    class FakeWinDLL:
        user32 = FakeLib("user32")
        shell32 = FakeLib("shell32")

    return FakeWinDLL(), calls


print("Windows-only startup paths")

import ctypes

from merlin import winicon
from merlin.brand import icon_path

path = icon_path()
check("logo file ships with the package", bool(path), path)

# --- winicon under a simulated Windows ---
real_name, real_windll = os.name, getattr(ctypes, "windll", None)
fake, calls = fake_windows_api()
try:
    os.name = "nt"
    ctypes.windll = fake
    if not hasattr(ctypes, "wintypes"):
        import ctypes.wintypes  # noqa: F401

    applied = winicon.apply_to_window(types.SimpleNamespace(winId=lambda: 12345),
                                      path)
    check("apply_to_window runs without error", applied is True)
    names = [c[0] for c in calls]
    check("LoadImageW called for both sizes", names.count("LoadImageW") == 2,
          f"calls={names.count('LoadImageW')}")
    check("SendMessageW pushes WM_SETICON", names.count("SendMessageW") == 2)
    icons = sorted(c[1][2] for c in calls if c[0] == "SendMessageW")
    check("both ICON_SMALL and ICON_BIG set", icons == [0, 1], str(icons))
    check("describe() reports success", "successfully" in winicon.describe(path))
finally:
    os.name = real_name
    if real_windll is None:
        del ctypes.windll
    else:
        ctypes.windll = real_windll

check("no-op again off Windows",
      winicon.apply_to_window(types.SimpleNamespace(winId=lambda: 1), path) is False)

# --- every name used in main() resolves, in order ---
import ast

source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "merlin", "app.py")).read()
tree = ast.parse(source)
main_fn = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "main")

imported_at = {}
used_at = {}
for node in ast.walk(main_fn):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            name = (alias.asname or alias.name).split(".")[0]
            imported_at.setdefault(name, node.lineno)
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        used_at.setdefault(node.id, node.lineno)

late = [n for n, line in imported_at.items()
        if n in used_at and used_at[n] < line]
check("nothing in main() is used before it is imported", not late, str(late))

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all startup paths OK")
