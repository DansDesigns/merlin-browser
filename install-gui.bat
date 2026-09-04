@echo off
rem Starts the graphical installer with a windowed interpreter, so no console
rem appears behind it. If anything goes wrong before the window opens, run
rem install.bat instead: it reports the same steps as text.
setlocal EnableExtensions

set "PY="
where pyw >nul 2>&1 && set "PY=pyw -3"
if not defined PY (where pythonw >nul 2>&1 && set "PY=pythonw")
if not defined PY (where py >nul 2>&1 && set "PY=py -3")
if not defined PY (where python >nul 2>&1 && set "PY=python")

if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)

start "" %PY% "%~dp0install-gui.py"
endlocal
