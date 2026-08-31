@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  Merlin Browser - Windows installer
rem
rem  Per-user install, no administrator rights required.
rem    program  ->  %LOCALAPPDATA%\Programs\Merlin  (with its own virtualenv,
rem                 so nothing is installed into your system Python)
rem    profile  ->  %APPDATA%\Merlin  (bookmarks, history, settings)
rem    cache    ->  %LOCALAPPDATA%\Merlin
rem  Remove it again with uninstall.bat, which leaves the profile alone unless
rem  you tell it not to.
rem ===========================================================================

title Merlin Browser installer
set "TOTAL_STEPS=7"

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "TARGET=%LOCALAPPDATA%\Programs\Merlin"
set "APPDIR=%TARGET%\app"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

echo.
echo  ===========================================================
echo    Merlin Browser
echo    Chromium engine, no Rust, real media player
echo  ===========================================================
echo.

if not exist "%SRC%\merlin\app.py" (
  echo  [X] Run this from the folder you extracted, next to the merlin folder.
  echo      Looked for: %SRC%\merlin\app.py
  echo.
  pause
  exit /b 1
)

rem --------------------------------------------------------------- 1. Python
rem The py launcher and a bare python.exe need different GUI variants:
rem   py -3  ->  pyw -3        python  ->  pythonw
set "PY="
set "PYW="
where py >nul 2>&1
if not errorlevel 1 (
  set "PY=py -3"
  set "PYW=pyw -3"
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    set "PY=python"
    set "PYW=pythonw"
  )
)
if not defined PY (
  echo  [X] Python was not found on this machine.
  echo.
  echo      Install it from https://www.python.org/downloads/ or run:
  echo          winget install Python.Python.3.12
  echo.
  echo      Tick "Add python.exe to PATH" in the installer, then run this again.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('%PY% -V 2^>^&1') do set "PYVER=%%v"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>nul
if errorlevel 1 (
  echo  [X] Python 3.9 or newer is required. Found %PYVER%.
  echo.
  pause
  exit /b 1
)
call :step 1 "Python %PYVER% found (used only to build the venv)"

rem ---------------------------------------------------- 2. isolated venv
rem Everything lives in its own virtualenv. Your system Python keeps whatever
rem it already had, and Merlin cannot break it or be broken by it.
set "VENV=%TARGET%\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

call :step 2 "Creating an isolated environment"
echo        %VENV%
if exist "%VENV%" rmdir /s /q "%VENV%"
mkdir "%TARGET%" 2>nul
echo        This takes a few seconds...
%PY% -m venv "%VENV%"
if errorlevel 1 goto :venvfail
if not exist "%VPY%" goto :venvfail
echo        Created.

call :step 3 "Installing PyQt6 and the web engine, about 150 MB"
echo        pip prints its own progress below. This is the slow part,
echo        usually one to three minutes depending on your connection.
echo.
echo        Updating pip inside the environment...
"%VPY%" -m pip install --quiet --upgrade pip >nul 2>&1
"%VPY%" -m pip install --progress-bar on PyQt6 PyQt6-WebEngine
if errorlevel 1 goto :pipfail
"%VPY%" -c "import PyQt6.QtWebEngineWidgets" >nul 2>&1
if errorlevel 1 goto :pipfail
echo        Engine installed and importable.

rem A normal Windows application is an .exe the shortcut points at directly.
rem pythonw.exe copied alongside its own DLLs inside the venv's Scripts folder
rem is exactly that: same interpreter, but its own process name, its own
rem taskbar identity, and something Windows can actually pin.
set "MERLINEXE=%VENV%\Scripts\Merlin.exe"
set "MERLINCON=%VENV%\Scripts\Merlin-console.exe"
copy /y "%VPYW%" "%MERLINEXE%" >nul 2>&1
copy /y "%VPY%"  "%MERLINCON%" >nul 2>&1
if not exist "%MERLINEXE%" (
  echo        Could not create Merlin.exe; falling back to pythonw.exe.
  set "MERLINEXE=%VPYW%"
)
if not exist "%MERLINCON%" set "MERLINCON=%VPY%"
goto :depsdone

:venvfail
echo.
echo  [X] Could not create the virtualenv. Check that the venv module exists:
echo          %PY% -m venv --help
echo.
pause
exit /b 1

:pipfail
echo.
echo  [X] Could not install PyQt6. Run this by hand to see why:
echo          "%VPY%" -m pip install PyQt6 PyQt6-WebEngine
echo.
pause
exit /b 1

:depsdone

rem ----------------------------------------------------------------- 4. copy
call :step 4 "Copying files to %TARGET%"
if exist "%APPDIR%" rmdir /s /q "%APPDIR%"
mkdir "%APPDIR%" 2>nul
xcopy /e /i /q /y "%SRC%\merlin" "%APPDIR%\merlin" >nul
if errorlevel 1 (
  echo  [X] Copy failed.
  pause
  exit /b 1
)
copy /y "%SRC%\merlin-run.py" "%APPDIR%\merlin-run.py" >nul
if errorlevel 1 (
  echo  [X] merlin-run.py is missing from the download.
  pause
  exit /b 1
)
rem the shortcut needs an .ico on disk; the app finds its own inside the package
if exist "%APPDIR%\merlin\merlin.ico" copy /y "%APPDIR%\merlin\merlin.ico" "%TARGET%\merlin.ico" >nul
if exist "%SRC%\README.md"     copy /y "%SRC%\README.md"     "%TARGET%\README.md"     >nul
if exist "%SRC%\uninstall.bat" copy /y "%SRC%\uninstall.bat" "%TARGET%\uninstall.bat" >nul

rem merlin-run.py puts its own folder on sys.path, so there is no .pth file,
rem no PYTHONPATH and no site-packages lookup to go wrong.
set "RUNPY=%APPDIR%\merlin-run.py"

> "%TARGET%\merlin-browser.cmd" (
  echo @echo off
  echo start "" "%MERLINEXE%" "%RUNPY%" %%*
)
> "%TARGET%\merlin-frameless.cmd" (
  echo @echo off
  echo start "" "%MERLINEXE%" "%RUNPY%" --no-decorations %%*
)
> "%TARGET%\merlin-debug.cmd" (
  echo @echo off
  echo echo Starting Merlin with a console attached. Any error appears below.
  echo echo.
  echo "%MERLINCON%" "%RUNPY%" %%*
  echo echo.
  echo pause
)
> "%TARGET%\merlin-console.cmd" (
  echo @echo off
  echo "%MERLINCON%" "%RUNPY%" %%*
  echo echo.
  echo pause
)
echo        Done.

rem prove it actually starts before promising the user a Start Menu entry
echo.
echo        Verifying that Merlin starts...
"%VPY%" "%RUNPY%" --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [X] Merlin will not start. The error was:
  echo.
  "%VPY%" "%RUNPY%" --version
  echo.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('""%VPY%" "%RUNPY%" --version"') do echo        %%v starts correctly.

rem ------------------------------------------------------------ 4. shortcuts
call :step 5 "Creating shortcuts"
set "ICO=%TARGET%\merlin.ico"
if not exist "%ICO%" set "ICO=%SystemRoot%\System32\shell32.dll,14"

choice /c YN /n /m "        Add a desktop shortcut as well? [Y/N] "
if errorlevel 2 (set "MERLIN_DESKTOP=0") else (set "MERLIN_DESKTOP=1")

rem Shortcuts point at Merlin.exe directly, with the script as a quoted
rem argument. A shortcut aimed at a .cmd cannot be pinned usefully: Windows
rem pins the batch file, and because it launches the browser through "start"
rem the pinned button has no process to attach to and appears to do nothing.
rem Quoting is done with [char]34 inside the generated script, so no escape has
rem to survive both cmd and PowerShell, which is what broke this before.
set "PS1=%TEMP%\merlin-shortcuts.ps1"
> "%PS1%" echo $ErrorActionPreference = 'Stop'
>>"%PS1%" echo $shell = New-Object -ComObject WScript.Shell
>>"%PS1%" echo $ico   = "%ICO%"
>>"%PS1%" echo $work  = "%APPDIR%"
>>"%PS1%" echo $menu  = "%STARTMENU%"
>>"%PS1%" echo $exe   = "%MERLINEXE%"
>>"%PS1%" echo $runpy = "%RUNPY%"
>>"%PS1%" echo $q     = [char]34
>>"%PS1%" echo function New-MerlinShortcut($path, $arguments, $description) {
>>"%PS1%" echo     $link = $shell.CreateShortcut($path)
>>"%PS1%" echo     $link.TargetPath       = $exe
>>"%PS1%" echo     $link.Arguments        = $arguments
>>"%PS1%" echo     $link.WorkingDirectory = $work
>>"%PS1%" echo     $link.IconLocation     = $ico
>>"%PS1%" echo     $link.WindowStyle      = 1
>>"%PS1%" echo     $link.Description      = $description
>>"%PS1%" echo     $link.Save()
>>"%PS1%" echo     $back = $shell.CreateShortcut($path)
>>"%PS1%" echo     if (-not (Test-Path $back.TargetPath)) { throw "target missing: " + $back.TargetPath }
>>"%PS1%" echo     Write-Output ("        " + [IO.Path]::GetFileName($path))
>>"%PS1%" echo     Write-Output ("            runs " + $back.TargetPath + " " + $back.Arguments)
>>"%PS1%" echo     Write-Output ("            icon " + $back.IconLocation)
>>"%PS1%" echo }
>>"%PS1%" echo New-MerlinShortcut ($menu + '\Merlin Browser.lnk') ($q + $runpy + $q) 'Merlin Browser'
>>"%PS1%" echo New-MerlinShortcut ($menu + '\Merlin Browser (Frameless).lnk') ($q + $runpy + $q + ' --no-decorations') 'Merlin with the title bar hidden'
>>"%PS1%" echo if ($env:MERLIN_DESKTOP -eq '1') {
>>"%PS1%" echo     New-MerlinShortcut ([Environment]::GetFolderPath('Desktop') + '\Merlin Browser.lnk') ($q + $runpy + $q) 'Merlin Browser'
>>"%PS1%" echo }

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 (
  echo.
  echo  [!] Shortcut creation failed, but Merlin itself is installed. Start it
  echo      from "%TARGET%\merlin-browser.cmd", or right-click that file and
  echo      choose Send to, Desktop to make your own shortcut.
  echo.
) else (
  echo        Each shortcut was read back and its target confirmed to exist.
)
del "%PS1%" >nul 2>&1

rem ----------------------------------------------------------------- 5. PATH
call :step 6 "Adding Merlin to your user PATH"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$p=[Environment]::GetEnvironmentVariable('Path','User'); if ($p -notlike '*%TARGET%*') { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';%TARGET%'), 'User') }" >nul 2>&1
echo        Open a new terminal before using the merlin-browser command.

rem --------------------------------------------------------------- 6. player
call :step 7 "Looking for a media player"
set "PLAYER="
where mpv >nul 2>&1 && set "PLAYER=mpv"
if defined PLAYER goto :haveplayer
where vlc >nul 2>&1 && set "PLAYER=vlc"
if defined PLAYER goto :haveplayer
set "VLC64=%ProgramFiles%\VideoLAN\VLC\vlc.exe"
if exist "%VLC64%" set "PLAYER=VLC"
if defined PLAYER goto :haveplayer
set "VLC32=%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"
if exist "%VLC32%" set "PLAYER=VLC"
if defined PLAYER goto :haveplayer

echo.
echo        No player found. This matters more on Windows than on Linux:
echo        the pip build of the engine cannot decode H.264, AAC or HEVC,
echo        and Windows has no distribution package that can. Without a
echo        player, most video will simply fail.
echo.
echo            winget install mpv.net
echo            winget install VideoLAN.VLC
echo.
echo        Merlin runs either as a separate process, so neither one puts
echo        any Rust inside the browser.
echo.
goto :playerdone

:haveplayer
echo        Found %PLAYER%. H.264, HEVC and AAC will play through it.
:playerdone

rem ------------------------------------------------------------------- done
echo.
echo  ===========================================================
echo    Installed to %TARGET%
echo  ===========================================================
echo.
echo    Launch           Start Menu, or:  merlin-browser
echo    No decorations   merlin-browser --no-decorations
echo    Codec report     merlin-console --codecs
echo    If it won't start      "%TARGET%\merlin-debug.cmd"
echo    Icon diagnostics       merlin-console --icon-check
echo    Uninstall        "%TARGET%\uninstall.bat"
echo.
echo    Python packages are confined to %VENV%
echo    Your system Python was not modified.
echo.

choice /c YN /n /m "  Start Merlin now? [Y/N] "
if errorlevel 2 goto :finish
start "" "%MERLINEXE%" "%RUNPY%"
:finish

echo.
echo  Done.
timeout /t 3 >nul
endlocal
exit /b 0

rem --------------------------------------------------------------------------
rem  :step <number> <label>
rem  Prints a heading and a progress bar, so the window is never just a cursor.
rem --------------------------------------------------------------------------
:step
setlocal EnableDelayedExpansion
set /a "num=%~1"
set /a "pct=num*100/%TOTAL_STEPS%"
set /a "filled=num*34/%TOTAL_STEPS%"
set "bar="
for /l %%i in (1,1,34) do (
  if %%i leq !filled! (set "bar=!bar!#") else (set "bar=!bar!.")
)
echo.
echo  [%~1/%TOTAL_STEPS%] %~2
echo  [!bar!] !pct!%%
endlocal
goto :eof
