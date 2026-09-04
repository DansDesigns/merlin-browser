@echo off
rem Starts the graphical uninstaller with a windowed interpreter, so no console
rem appears behind it. If it will not start, run uninstall.bat instead: it does
rem the same work and reports it as text.
setlocal EnableExtensions

set "PY="
where pyw >nul 2>&1 && set "PY=pyw -3"
if not defined PY (where pythonw >nul 2>&1 && set "PY=pythonw")
if not defined PY (where py >nul 2>&1 && set "PY=py -3")
if not defined PY (where python >nul 2>&1 && set "PY=python")

if not defined PY (
  echo Python was not found, so the graphical uninstaller cannot run.
  echo Run uninstall.bat instead.
  pause
  exit /b 1
)

start "" %PY% "%~dp0uninstall-gui.py"
endlocal
