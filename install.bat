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
set "CUR_STEP=0"
set "CUR_LABEL=Starting"

rem Grab an ESC character so the bar can be pinned to the bottom line with a
rem scroll region, the same way the Linux installer does it. Windows 10 and 11
rem consoles understand these; if yours does not, VT_OK stays 0 and the bar is
rem simply printed under each step instead.
set "ESC="
for /f %%e in ('echo prompt $E ^| cmd') do set "ESC=%%e"
set "VT_OK=0"
if defined ESC set "VT_OK=1"
rem The window height, not the buffer height. "mode con" reports Lines: 9001
rem for the scrollback buffer, and reserving line 9001 puts the bar somewhere
rem off screen, which is why it vanished as soon as output scrolled.
set "ROWS="
for /f "usebackq tokens=*" %%r in (`powershell -NoProfile -Command "$Host.UI.RawUI.WindowSize.Height" 2^>nul`) do set "ROWS=%%r"
if not defined ROWS set "ROWS=25"
if %ROWS% LSS 10 set "ROWS=25"
set /a "SCROLL_ROWS=ROWS-1"
if "%VT_OK%"=="1" (
  rem The scrolling region must END one line above the bar. Setting it to
  rem 1;ROWS included the bar's own line, so pip's output scrolled straight
  rem over it and the bar was lost as soon as anything long printed.
  <nul set /p "=%ESC%[1;%SCROLL_ROWS%r"
  <nul set /p "=%ESC%[1;1H"
)

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "TARGET=%LOCALAPPDATA%\Programs\Merlin"
set "APPDIR=%TARGET%\app"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

echo.
echo  ===========================================================
echo    Merlin Browser
echo    Chromium engine, tabbed, with a built-in content blocker
echo  ===========================================================
echo.
echo   If this window closes on its own, open a Command Prompt, change to
echo   this folder and run install.bat from there: the error stays on screen.
echo.

rem Plain findstr, no nested quoting. brand.py contains: APP_VERSION = "1.5.7"
rem so token 3 is the quoted version and %%~v strips the quotes.
set "SRCVER="
for /f "tokens=3" %%v in ('findstr /b /c:"APP_VERSION = " "%SRC%\merlin\brand.py" 2^>nul') do set "SRCVER=%%~v"
if defined SRCVER echo   Installing version %SRCVER% from %SRC%
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
if exist "%VENV%" (
  echo        Removing the previous environment first...
  rmdir /s /q "%VENV%"
)
mkdir "%TARGET%" 2>nul
echo        This takes a few seconds.
echo        $ %PY% -m venv "%VENV%"
%PY% -m venv "%VENV%"
if errorlevel 1 goto :venvfail
if not exist "%VPY%" goto :venvfail
echo        Created.
call :bar

call :step 3 "Installing PyQt6 and the web engine, about 150 MB"
echo        pip prints its own progress below. This is the slow part,
echo        usually one to three minutes depending on your connection.
echo.
echo        Updating pip inside the environment
echo        $ "%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install --upgrade pip
call :bar
echo.
echo        Downloading and installing PyQt6 and Qt WebEngine
echo        $ "%VPY%" -m pip install PyQt6 PyQt6-WebEngine
"%VPY%" -m pip install --progress-bar on PyQt6 PyQt6-WebEngine
if errorlevel 1 goto :pipfail
call :bar
echo.
echo        Checking the engine imports
"%VPY%" -c "import PyQt6.QtWebEngineWidgets; from PyQt6.QtCore import QT_VERSION_STR; print('        Qt ' + QT_VERSION_STR + ' ready')"
if errorlevel 1 goto :pipfail

goto :depsdone

:venvfail
call :endui
echo.
echo  [X] Could not create the virtualenv. Check that the venv module exists:
echo          %PY% -m venv --help
echo.
pause
exit /b 1

:pipfail
call :endui
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
echo        $ xcopy "%SRC%\merlin" "%APPDIR%\merlin"
xcopy /e /i /y "%SRC%\merlin" "%APPDIR%\merlin" | findstr /v /c:"File(s) copied"
if errorlevel 1 (
  call :endui
  echo  [X] Copy failed.
  pause
  exit /b 1
)
copy /y "%SRC%\merlin-run.py" "%APPDIR%\merlin-run.py" >nul
if errorlevel 1 (
  call :endui
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

rem Merlin.exe: a copy of the interpreter, so the browser runs as its own
rem application rather than as Python. Built here, after the app files exist,
rem because the icon has to be written into it straight away and RUNPY only
rem exists at this point.
set "MERLINEXE=%VENV%\Scripts\Merlin.exe"
set "MERLINCON=%VENV%\Scripts\Merlin-console.exe"
echo.
echo        Creating Merlin.exe
copy /y "%VPYW%" "%MERLINEXE%" >nul 2>&1
copy /y "%VPY%"  "%MERLINCON%" >nul 2>&1
if not exist "%MERLINEXE%" (
  echo.
  echo  [!] Merlin.exe could not be created. Windows will show the Python icon
  echo      because the browser will be running as pythonw.exe. This is almost
  echo      always antivirus blocking the copy of an executable.
  echo      Allow %VENV%\Scripts and run this installer again.
  echo.
  set "MERLINEXE=%VPYW%"
  set "MERLINCON=%VPY%"
) else (
  echo        Created %MERLINEXE%
  rem Nothing has run Merlin.exe yet, so it cannot be locked: write the icon in
  rem now, before the verification step below executes it for the first time.
  echo        Writing the Merlin icon into it
  "%VPY%" "%RUNPY%" --embed-icon "%MERLINEXE%"
  if errorlevel 1 (
    echo.
    echo  [!] The icon could not be written into Merlin.exe. The taskbar will
    echo      fall back to the interpreter's icon. Settings, Advanced shows the
    echo      current state.
    echo.
  )
)
call :bar


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
echo        Verifying that Merlin starts
echo        $ "%MERLINCON%" "%RUNPY%" --version
"%MERLINCON%" "%RUNPY%" --version
if errorlevel 1 (
  echo.
  call :endui
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

rem Refresh any existing pinned taskbar entry as well. Pinning copies the
rem shortcut into User Pinned\TaskBar, which the installer has never touched,
rem so a pin made against an older build kept pointing at the old target and
rem showing the old icon however many times Merlin was reinstalled.
>>"%PS1%" echo $pinned = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
>>"%PS1%" echo if (Test-Path $pinned) {
>>"%PS1%" echo     Get-ChildItem -Path $pinned -Filter *.lnk ^| ForEach-Object {
>>"%PS1%" echo         $existing = $shell.CreateShortcut($_.FullName)
>>"%PS1%" echo         if ($existing.TargetPath -like '*Merlin*' -or $existing.Arguments -like '*merlin-run.py*') {
>>"%PS1%" echo             $existing.TargetPath   = $exe
>>"%PS1%" echo             $existing.Arguments    = $q + $runpy + $q
>>"%PS1%" echo             $existing.WorkingDirectory = $work
>>"%PS1%" echo             $existing.IconLocation = $ico
>>"%PS1%" echo             $existing.Save()
>>"%PS1%" echo             Write-Output ("        refreshed pinned entry " + $_.Name)
>>"%PS1%" echo         }
>>"%PS1%" echo     }
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
call :bar
)
del "%PS1%" >nul 2>&1

rem ------------------------------------------------------- icon cache
rem Windows keeps a cache of icons keyed by executable path. Merlin.exe existed
rem in earlier installs carrying the interpreter's icon, so the cache holds that
rem against this path and keeps serving it even though the file now has the
rem right icon embedded and the shortcut points at it. Rebuilding the cache is
rem the only thing that shifts it.
echo.
echo        Refreshing the Windows icon cache
ie4uinit.exe -show >nul 2>&1
if errorlevel 1 ie4uinit.exe -ClearIconCache >nul 2>&1
del /f /q "%LOCALAPPDATA%\IconCache.db" >nul 2>&1
del /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*.db" >nul 2>&1
call :bar

echo.
echo        Explorer has to restart to pick up the rebuilt cache. Your windows
echo        and files stay open; only the taskbar and desktop redraw.
choice /c YN /n /m "        Restart Explorer now? [Y/N] "
if errorlevel 2 (
  echo        Skipped. If the taskbar still shows the old icon, sign out and
  echo        back in, or run: ie4uinit.exe -show
) else (
  taskkill /f /im explorer.exe >nul 2>&1
  timeout /t 1 >nul
  start explorer.exe
  echo        Explorer restarted.
)
call :bar

rem ----------------------------------------------------------------- 5. PATH
call :step 6 "Adding Merlin to your user PATH"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$p=[Environment]::GetEnvironmentVariable('Path','User'); if ($p -notlike '*%TARGET%*') { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';%TARGET%'), 'User') }" >nul 2>&1
echo        Open a new terminal before using the merlin-browser command.
call :bar

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
echo    Installed version %SRCVER% to %TARGET%
echo  ===========================================================
echo.
echo    Launch           Start Menu, or:  merlin-browser
echo    No decorations   merlin-browser --no-decorations
echo    Codec report     merlin-console --codecs
echo    If it won't start      "%TARGET%\merlin-debug.cmd"
echo.
echo    If a pinned taskbar entry still shows the old icon, unpin and re-pin
echo    it once. Windows caches icons against the pinned shortcut as well as
echo    the executable, and only re-pinning clears that one.
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

call :endui
echo.
echo  Done.
echo.
pause
endlocal
exit /b 0

rem --------------------------------------------------------------------------
rem  :bar   redraw the pinned bar with the current step and label
rem --------------------------------------------------------------------------
:bar
setlocal EnableDelayedExpansion
set /a "num=%CUR_STEP%"
set /a "pct=num*100/%TOTAL_STEPS%"
set /a "filled=num*34/%TOTAL_STEPS%"
set "track="
for /l %%i in (1,1,34) do (
  if %%i leq !filled! (set "track=!track!#") else (set "track=!track!.")
)
if "%VT_OK%"=="1" (
  <nul set /p "=!ESC!7!ESC![%ROWS%;1H!ESC![2K  [!track!] !pct!%%  %CUR_LABEL%!ESC!8"
)
title Merlin installer  [!track!] !pct!%%  -  %CUR_LABEL%
endlocal
goto :eof

rem --------------------------------------------------------------------------
rem  :step <number> <label>
rem  Heading in the scrolling area, bar pinned on the reserved bottom line.
rem --------------------------------------------------------------------------
:step
set "CUR_STEP=%~1"
set "CUR_LABEL=%~2"
echo.
echo  [%~1/%TOTAL_STEPS%] %~2
call :bar
goto :eof

rem --------------------------------------------------------------------------
rem  :endui   release the scroll region and clear the pinned line
rem --------------------------------------------------------------------------
:endui
if "%VT_OK%"=="1" (
  <nul set /p "=%ESC%7%ESC%[%ROWS%;1H%ESC%[2K%ESC%8%ESC%[r"
)
title Merlin Browser installer
goto :eof
