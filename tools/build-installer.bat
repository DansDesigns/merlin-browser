@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem ===========================================================================
rem  build-installer.bat
rem
rem  Produces MerlinSetup.exe: the graphical installer with everything it needs
rem  packed inside it, so there is one file to hand someone rather than a zip
rem  to extract first.
rem
rem  Same method as Merlin.exe itself, PyInstaller with the icon in the
rem  executable's resources. Run it on Windows, from the project folder: a
rem  Windows executable has to be built on Windows.
rem ===========================================================================

title Building MerlinSetup.exe

pushd "%~dp0.."
set "SRC=%CD%"

echo.
echo   Building MerlinSetup.exe from %SRC%
echo.

if not exist "%SRC%\install-gui.py" (
  echo  [X] install-gui.py is not here. Run this from the tools folder of the
  echo      project, not from a copy.
  goto :fail
)

rem The Store build of Python cannot produce a usable executable for the same
rem reason Merlin avoids it: package identity. Prefer a normal one.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (
  echo  [X] Python was not found. Install it from python.org first.
  goto :fail
)

set "PYBASE="
for /f "usebackq delims=" %%b in (`%PY% -c "import sys;print(sys.base_prefix)" 2^>nul`) do set "PYBASE=%%b"
echo %PYBASE% | findstr /i "WindowsApps" >nul
if not errorlevel 1 (
  set "ALTPY="
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%d\python.exe" set "ALTPY=%%d\python.exe"
  )
  if defined ALTPY (
    echo   Store Python found; using !ALTPY! instead.
    set "PY=!ALTPY!"
  ) else (
    echo  [!] Only the Microsoft Store Python is installed. The built installer
    echo      will work, but install one from python.org for a cleaner result.
  )
)

echo   Installing PyInstaller...
%PY% -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto :fail

set "BUILDDIR=%TEMP%\merlin-setup-build"
if exist "%BUILDDIR%" rmdir /s /q "%BUILDDIR%"
if exist "%SRC%\dist\MerlinSetup.exe" del /f /q "%SRC%\dist\MerlinSetup.exe"

echo.
echo   Building. Two or three minutes.
echo.
%PY% -m PyInstaller --noconfirm --onefile --windowed --name MerlinSetup ^
  --icon "%SRC%\merlin\merlin.ico" ^
  --add-data "%SRC%\install.bat;." ^
  --add-data "%SRC%\install.sh;." ^
  --add-data "%SRC%\uninstall.bat;." ^
  --add-data "%SRC%\merlin-run.py;." ^
  --add-data "%SRC%\version.txt;." ^
  --add-data "%SRC%\requirements.txt;." ^
  --add-data "%SRC%\merlin;merlin" ^
  --distpath "%SRC%\dist" --workpath "%BUILDDIR%" --specpath "%BUILDDIR%" ^
  "%SRC%\install-gui.py"
if errorlevel 1 goto :fail
if not exist "%SRC%\dist\MerlinSetup.exe" goto :fail

echo   Cleaning up...
if exist "%BUILDDIR%" rmdir /s /q "%BUILDDIR%"

echo.
echo   ===========================================================
echo    Built %SRC%\dist\MerlinSetup.exe
echo   ===========================================================
echo.
echo   One file, nothing to extract. It carries install.bat and the merlin
echo   package inside it and unpacks them to a temporary folder when it runs.
echo.
popd
pause
endlocal
exit /b 0

:fail
echo.
echo  [X] The installer was not built. The output above says why.
echo.
popd
pause
endlocal
exit /b 1
