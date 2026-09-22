#!/usr/bin/env python3
"""Regenerate merlin/stdlib_anchor.py from the running Python's module list."""
import os
import sys

SKIP = {'idlelib', 'venv', 'tkinter', 'curses', 'lib2to3', 'readline', '__phello__', 'pydoc', 'distutils', 'msilib', 'pydoc_data', 'winsound', 'ensurepip', 'crypt', 'tabnanny', 'tty', 'spwd', 'test', 'turtle', 'antigravity', 'pty', 'sre_compile', 'ossaudiodev', 'this', 'sre_constants', 'nis', 'turtledemo', 'dbm', 'sre_parse'}

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(HERE, "merlin", "stdlib_anchor.py")

names = sorted(n for n in sys.stdlib_module_names
               if not n.startswith("_") and n not in SKIP)
names.append("__future__")
source = open(TARGET, encoding="utf-8").read()
head = source.split('if __name__ == "__merlin_never_runs__":')[0]
body = "\n".join(f"    import {n}  # noqa: F401" for n in sorted(set(names)))
with open(TARGET, "w", encoding="utf-8") as handle:
    handle.write(head + 'if __name__ == "__merlin_never_runs__":\n' + body + "\n")
print(f"{TARGET}: {len(set(names))} modules")
