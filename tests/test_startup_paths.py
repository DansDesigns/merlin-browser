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

# --- icon resource structures, which the installer writes into Merlin.exe ---
from merlin.winexe import build_group, parse_ico

with open(path, "rb") as fh:
    ico = fh.read()
images = parse_ico(ico)
check("icon parses into images", len(images) >= 5, f"{len(images)} entries")
check("payload sizes match the directory",
      all(len(payload) == entry["size"] for entry, payload in images))
group = build_group(images)
check("group directory is 6 + 14n bytes", len(group) == 6 + 14 * len(images))
rejected = 0
for blob in (b"", b"\x01\x00\x02\x00\x01\x00", ico[:10], ico[:len(ico)//2]):
    try:
        parse_ico(blob)
    except ValueError:
        rejected += 1
check("malformed icons are rejected, not crashed on", rejected == 4)

# --- web app shortcut text is built without touching the filesystem ---
from merlin import webapps

check("slugify strips path-hostile characters",
      webapps.slugify("a/b c:d") == "a-b-c-d", webapps.slugify("a/b c:d"))
check("powershell literal escapes quotes",
      webapps._ps("it's") == "'it''s'", webapps._ps("it's"))

# --- proxy flags must survive being packed into one env var -----------------
from merlin import privacy

flags = privacy.proxy_flags("socks5://127.0.0.1:9050")
check("proxy flags contain no spaces inside a value",
      all(" " not in f for f in flags), str(flags))
check("no proxy flags when no proxy", privacy.proxy_flags("") == [])
check("tor url is empty when no daemon is running",
      privacy.tor_proxy_url(0) == "" or privacy.find_tor_port() != 0)

# --- web app shortcuts are passed to PowerShell without a temp file ---------
import base64

from merlin import webapps

sample = "$link.Arguments = " + webapps._ps('"C:\\a b\\run.py" --app "https://x"')
encoded = base64.b64encode(sample.encode("utf-16-le")).decode("ascii")
check("encoded command round trips",
      base64.b64decode(encoded).decode("utf-16-le") == sample)

import re as _re

root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# --- batch quoting hazards --------------------------------------------------
# A PowerShell call with \" escapes inside a for /f broke install.bat twice:
# cmd has no backslash escape, so the quotes ended the string early and the
# rest of the line was parsed as commands.
def batch_quoting_problems(text):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("rem") or not stripped:
            continue
        # `if "%VAR:~-1%"=="\"` is the trailing-backslash idiom, not an escape
        if '\\"' in line and '=="\\"' not in line.replace(" ", ""):
            found.append((number, "backslash-escaped quote"))
        if "for /f" in line:
            inner = line.split("(", 1)[-1]
            for match in _re.finditer(r"\^(.)", inner):
                if match.group(1) not in "<>&|^":
                    found.append((number, f"caret eats {match.group(1)!r}"))
    return found


for name in ("install.bat", "uninstall.bat"):
    text = open(os.path.join(root, name), encoding="utf-8").read()
    issues = batch_quoting_problems(text)
    check(f"{name} has no cmd quoting hazards", not issues, str(issues))

# Nothing shipped may delete, rewrite or restart anything it does not own. An
# installer that stamped its identity onto every pinned shortcut merged other
# applications into Merlin and replaced their icons.
for name in ("install.bat", "uninstall.bat"):
    text = open(os.path.join(root, name), encoding="utf-8").read()
    commands = "\n".join(l for l in text.splitlines()
                         if not l.strip().lower().startswith("rem"))

    # reading the pinned folder is fine; writing to a shortcut that is not
    # ours is not, so any write must be guarded by both of these conditions
    if "User Pinned" in commands:
        # the only write is clearing our own id, and only from shortcuts that
        # actually carry it
        check(f"{name} only touches shortcuts carrying Merlin's own id",
              "$current -eq $id" in commands)
        check(f"{name} never sets an application id on a shortcut",
              "v.vt = 31" not in commands and "Tag]::Apply" not in commands)
        # writing an id to our own named shortcuts is fine; writing to one
        # that came out of a listing is what damaged other applications
        writes = [l for l in commands.splitlines()
                  if "Tag]::Apply" in l or "Stamp]::Apply" in l]
        check(f"{name} never tags an enumerated shortcut",
              not any("Get-ChildItem" in l or "$_." in l for l in writes),
              str(writes)[:90])
        check(f"{name} tags only shortcuts it names itself",
              all("Merlin Browser" in l or "$own" in l or "$lnk" in l
                  for l in writes) if writes else True)
    # deleting a shortcut we created by name is fine; deleting one that came
    # out of a directory listing means deleting somebody else's
    enumerated = _re.findall(r'Remove-Item\s+(\$_[^\s;)]*)', commands)
    piped = [l for l in commands.splitlines()
             if "Remove-Item" in l and "Get-ChildItem" in l]
    check(f"{name} never deletes an enumerated shortcut",
          not enumerated and not piped, str(enumerated or piped)[:80])

    # process control: only Merlin's own process, and never the shell
    kills = _re.findall(r'taskkill[^\n]*?/im\s+(\S+)', commands)
    check(f"{name} only ever closes Merlin.exe",
          all(k.lower().startswith("merlin") for k in kills), str(kills))
    check(f"{name} never starts or stops Explorer",
          "start explorer" not in commands.lower()
          and not _re.search(r'taskkill[^\n]*explorer', commands, _re.I))

# --- installer variables must be set before they are used -------------------

def first_use_before_set(text, names, set_pattern, use_pattern):
    late = []
    for name in names:
        sets = [m.start() for m in _re.finditer(set_pattern.format(n=name), text)]
        uses = [m.start() for m in _re.finditer(use_pattern.format(n=name), text)]
        if uses and (not sets or min(uses) < min(sets)):
            late.append(name)
    return late


bat = open(os.path.join(root, "install.bat"), encoding="utf-8").read()
bat_names = sorted(set(_re.findall(r'set "([A-Z_][A-Z0-9_]*)=', bat)))
late_bat = first_use_before_set(
    bat, bat_names, r'set "{n}=', r'%{n}%')
# CUR_STEP and CUR_LABEL are written by :step and read by :bar, which is a
# subroutine defined after them; that ordering is fine.
late_bat = [n for n in late_bat if n not in ("CUR_STEP", "CUR_LABEL", "ESC")]
check("install.bat sets every variable before using it", not late_bat,
      str(late_bat))

# install.sh is executed during testing, so a syntax check is enough here;
# install.bat cannot be run on this platform, which is why it gets the static
# variable-order check above. That check exists because an empty %RUNPY% was
# passed to the icon step, and python read the working directory as a script.
import subprocess as _sp

sh_path = os.path.join(root, "install.sh")
syntax = _sp.run(["bash", "-n", sh_path], capture_output=True)
check("install.sh parses", syntax.returncode == 0,
      syntax.stderr.decode()[:120])

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all startup paths OK")
