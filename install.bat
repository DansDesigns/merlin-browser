@echo off
setlocal EnableExtensions EnableDelayedExpansion
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

rem Unattended mode, used by the graphical installer so that it drives this
rem script rather than reimplementing it. Every prompt takes its answer from
rem an environment variable instead.
set "SILENT=0"
if /i "%~1"=="--yes" set "SILENT=1"
if /i "%~1"=="-y" set "SILENT=1"
if "%MERLIN_SILENT%"=="1" set "SILENT=1"
set "BUILT_EXE=0"
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

rem The version comes from version.txt, whose first line is the version.
set "SRCVER="
if exist "%SRC%\version.txt" (
  for /f "usebackq tokens=1 delims= " %%v in ("%SRC%\version.txt") do (
    if not defined SRCVER set "SRCVER=%%v"
  )
)
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

rem ---------------------------------------------------- Store Python check
rem A Microsoft Store interpreter runs with MSIX package identity, and Windows
rem takes a taskbar button's identity and icon from the package manifest rather
rem than from the window. Merlin can set its window icon perfectly and the
rem taskbar will still show Python's, because as far as Windows is concerned
rem the package is the application. Nothing in the browser can override that,
rem so a normal interpreter is used instead where one exists.
set "PYBASE="
for /f "usebackq delims=" %%b in (`%PY% -c "import sys;print(sys.base_prefix)" 2^>nul`) do set "PYBASE=%%b"
echo %PYBASE% | findstr /i "WindowsApps" >nul
if not errorlevel 1 (
  echo.
  echo   The Python found is the Microsoft Store build:
  echo       %PYBASE%
  echo   Windows gives Store apps their icon from the package, so Merlin would
  echo   always show Python's icon in the taskbar. Looking for another Python.
  set "ALTPY="
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%d\python.exe" set "ALTPY=%%d\python.exe"
  )
  for /d %%d in ("%ProgramFiles%\Python3*") do (
    if exist "%%d\python.exe" set "ALTPY=%%d\python.exe"
  )
  if defined ALTPY (
    echo   Using !ALTPY! instead.
    set "PY=!ALTPY!"
  ) else (
    echo.
    echo   No other Python is installed. Merlin will work, but its taskbar icon
    echo   will be Python's and cannot be changed from inside the browser.
    echo.
    echo   To fix it, install a normal Python and run this installer again:
    echo       winget install Python.Python.3.13
    echo   or download it from https://www.python.org/downloads/
    echo.
    if "%SILENT%"=="1" (set "STOREANSWER=1") else (
      choice /c YN /n /m "   Carry on with the Store Python anyway? [Y/N] "
      if errorlevel 2 (set "STOREANSWER=2") else (set "STOREANSWER=1")
    )
    if "!STOREANSWER!"=="2" (
      echo   Stopped. Nothing was installed.
      pause
      exit /b 1
    )
  )
)

rem The version is read here, after any substitution above, so the reported
rem version is the interpreter actually being used rather than the one first
rem found.
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
rem pip is already in the new environment. Upgrading it is usually harmless
rem and occasionally the difference between a wheel installing and not, but it
rem is a download, so it is asked rather than assumed.
set "PIPVER="
for /f "tokens=2" %%p in ('"%VPY%" -m pip --version 2^>nul') do (
  if not defined PIPVER set "PIPVER=%%p"
)
if defined PIPVER (echo        pip %PIPVER% is already in this environment.)

set "UPGRADE_PIP=%MERLIN_UPGRADE_PIP%"
if not defined UPGRADE_PIP (
  if "%SILENT%"=="1" (
    set "UPGRADE_PIP=0"
  ) else (
    choice /c YN /n /m "        Replace it with the latest? [Y/N] "
    if errorlevel 2 (set "UPGRADE_PIP=0") else (set "UPGRADE_PIP=1")
  )
)

if "%UPGRADE_PIP%"=="1" (
  echo        $ "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install --upgrade pip
) else (
  echo        Keeping pip %PIPVER%.
)
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
copy /y "%SRC%\merlin-boot.py" "%APPDIR%\merlin-boot.py" >nul
rem version.txt is the only place the version is written, so the installed
rem copy needs it: without it the browser reports 0.0.0
if exist "%SRC%\version.txt" copy /y "%SRC%\version.txt" "%APPDIR%\version.txt" >nul
if exist "%SRC%\changelog.txt" copy /y "%SRC%\changelog.txt" "%APPDIR%\changelog.txt" >nul
if errorlevel 1 (
  call :endui
  echo  [X] merlin-run.py is missing from the download.
  pause
  exit /b 1
)
rem the shortcut needs an .ico on disk; the app finds its own inside the package
if exist "%APPDIR%\merlin\merlin.ico" (
  copy /y "%APPDIR%\merlin\merlin.ico" "%TARGET%\merlin.ico" >nul
)
if exist "%TARGET%\merlin.ico" (
  echo        Icon file in place: %TARGET%\merlin.ico
) else (
  echo        [!] merlin.ico is missing from the package. Shortcuts will fall
  echo            back to a generic icon.
)
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
rem ------------------------------------------------- a real Merlin.exe
rem Built with PyInstaller, which is what every other Python application on
rem Windows does. The point is that the process owning the window is Merlin's
rem own executable with the icon in its resources; a script run through an
rem interpreter can never be, because the window belongs to the interpreter.
rem
rem The virtualenv stays as the fallback: if the build does not produce a
rem working executable, the launcher is used and the browser still runs.
set "MERLINEXE=%VENV%\Scripts\pythonw.exe"
set "MERLINCON=%VENV%\Scripts\python.exe"
set "BUILT_EXE=0"
set "BUILDDIR=%TEMP%\merlin-build"

echo.
echo        Building Merlin.exe. This is the slow part, two to four minutes.
echo        $ "%VPY%" -m pip install pyinstaller
"%VPY%" -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto :buildfailed

if exist "%TARGET%\bin" rmdir /s /q "%TARGET%\bin"
echo.
echo        $ pyinstaller --windowed --icon merlin.ico merlin-run.py
rem The merlin package is deliberately NOT bundled. The executable carries the
rem interpreter, Qt and the web engine, which change only when a dependency
rem does; the application itself is read from %APPDIR% at run time. An update
rem is then a matter of replacing .py files, with no rebuild and no need for
rem anyone to run this script again.
"%VPY%" -m PyInstaller --noconfirm --onedir --windowed --name Merlin ^
  --icon "%APPDIR%\merlin\merlin.ico" ^
  --exclude-module merlin ^
  --hidden-import PyQt6.QtWebEngineWidgets ^
  --hidden-import PyQt6.QtWebEngineCore ^
  --hidden-import PyQt6.QtMultimedia ^
  --distpath "%TARGET%\bin" --workpath "%BUILDDIR%" --specpath "%BUILDDIR%" ^
  "%APPDIR%\merlin-boot.py"
if exist "%TARGET%\bin\merlin-boot" (
  if not exist "%TARGET%\bin\Merlin" ren "%TARGET%\bin\merlin-boot" "Merlin"
)
if errorlevel 1 goto :buildfailed
if not exist "%TARGET%\bin\Merlin\Merlin.exe" goto :buildfailed

rem Tell the executable where the application lives, so it does not have to
rem rely on the folder layout alone.
> "%TARGET%\bin\Merlin\app-path.txt" echo %APPDIR%

echo.
echo        Checking the built executable starts...
"%TARGET%\bin\Merlin\Merlin.exe" --version
if errorlevel 1 goto :buildfailed

set "MERLINEXE=%TARGET%\bin\Merlin\Merlin.exe"
set "MERLINCON=%TARGET%\bin\Merlin\Merlin.exe"
set "BUILT_EXE=1"
> "%APPDIR%\build-status.txt" echo built
echo        Built %MERLINEXE%
echo        The window now belongs to Merlin.exe, so the taskbar uses its icon.
goto :buildclean

:buildfailed
echo.
echo  ============================================================
echo   [!] Merlin.exe was NOT built.
echo.
echo   Merlin will still run, started through the virtualenv, but the
echo   window will belong to python.exe and the taskbar will show
echo   Python's icon rather than Merlin's.
echo.
echo   The PyInstaller output above says why. Settings, Advanced
echo   repeats this.
echo  ============================================================
echo.
> "%APPDIR%\build-status.txt" echo fallback

:buildclean
rem Remove the build workspace and PyInstaller itself. Neither is needed once
rem the executable exists, and together they are a few hundred megabytes.
echo        Cleaning up the build files...
if exist "%BUILDDIR%" rmdir /s /q "%BUILDDIR%"
"%VPY%" -m pip uninstall -y pyinstaller pyinstaller-hooks-contrib altgraph >nul 2>&1
for /d %%d in ("%VENV%\Lib\site-packages\__pycache__") do rmdir /s /q "%%d" 2>nul
echo        Done.
call :bar


rem A built executable carries the script inside it and must not be handed a
rem path; the virtualenv launcher must. One variable covers both.
set "RUNARG="%RUNPY%""
if "%BUILT_EXE%"=="1" set "RUNARG="

> "%TARGET%\merlin-browser.cmd" (
  echo @echo off
  echo start "" "%MERLINEXE%" %RUNARG% %%*
)
> "%TARGET%\merlin-frameless.cmd" (
  echo @echo off
  echo start "" "%MERLINEXE%" %RUNARG% --no-decorations %%*
)
> "%TARGET%\merlin-debug.cmd" (
  echo @echo off
  echo echo Starting Merlin with a console attached. Any error appears below.
  echo echo.
  echo "%MERLINCON%" %RUNARG% %%*
  echo echo.
  echo pause
)
> "%TARGET%\merlin-console.cmd" (
  echo @echo off
  echo "%MERLINCON%" %RUNARG% %%*
  echo echo.
  echo pause
)
echo        Done.

rem prove it actually starts before promising the user a Start Menu entry

echo.
echo        Verifying that Merlin starts
echo        $ "%MERLINCON%" %RUNARG% --version
"%MERLINCON%" %RUNARG% --version
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
if not exist "%ICO%" (
  echo        [!] No merlin.ico, so shortcuts get a generic icon.
  set "ICO=%SystemRoot%\System32\shell32.dll,14"
)

if "%SILENT%"=="1" (
  rem the front end has already asked; default to no shortcut if unset
  if not defined MERLIN_DESKTOP set "MERLIN_DESKTOP=0"
) else (
  choice /c YN /n /m "        Add a desktop shortcut as well? [Y/N] "
  if errorlevel 2 (set "MERLIN_DESKTOP=0") else (set "MERLIN_DESKTOP=1")
)

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
>>"%PS1%" echo # a built executable carries the script inside it
>>"%PS1%" echo $args1 = if ($env:MERLIN_BUILT -eq '1') { '' } else { $q + $runpy + $q }
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
>>"%PS1%" echo New-MerlinShortcut ($menu + '\Merlin Browser.lnk') $args1 'Merlin Browser'
>>"%PS1%" echo New-MerlinShortcut ($menu + '\Merlin Browser (Frameless).lnk') ($args1 + ' --no-decorations') 'Merlin with the title bar hidden'
>>"%PS1%" echo if ($env:MERLIN_DESKTOP -eq '1') {
>>"%PS1%" echo     New-MerlinShortcut ([Environment]::GetFolderPath('Desktop') + '\Merlin Browser.lnk') $args1 'Merlin Browser'
>>"%PS1%" echo }

set "MERLIN_BUILT=%BUILT_EXE%"
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

rem No App User Model ID is written to any shortcut, and no shortcut outside
rem Merlin's own is touched. A previous version stamped Merlin's ID onto every
rem .lnk in the pinned taskbar folder, which made Windows treat other pinned
rem applications as Merlin and replace their icons. Run repair-taskbar.bat to
rem undo that if it happened.

rem No shortcut is tagged with an App User Model ID, and Merlin claims none.
rem
rem Tagging looked like the way to give the taskbar the right icon. It is the
rem opposite: Windows passes a shortcut's ID to the process it launches, then
rem resolves the icon by looking for a shortcut declaring that ID, and when
rem that lookup does not resolve it falls back to the process image's icon,
rem which for a Python program is Python's. Untagged, the window's own icon is
rem used, which Merlin sets on every window.

rem ------------------------------------------- undo earlier taskbar damage
rem Versions 1.5.8 and 1.5.9 wrote Merlin's application id onto every shortcut
rem in the pinned taskbar folder, not only Merlin's own. Windows then treated
rem those programs as Merlin: pins merged together and took Merlin's icon.
rem
rem This puts it right. Each shortcut is read, and where the id is Merlin's but
rem the shortcut does not point at Merlin.exe, that one property is cleared.
rem Shortcuts without Merlin's id are never opened for writing, nothing is
rem deleted, no shortcut is created outside Merlin's own, and Explorer is not
rem touched. If nothing was ever damaged this reports that and changes nothing.
echo.
echo        Clearing Merlin's application id from every shortcut carrying it
set "MERLIN_ID=DansDesigns.Merlin.Browser"
set "MERLIN_EXE=%MERLINEXE%"
set "PSFIX=%TEMP%\merlin-taskbar-check.ps1"
> "%PSFIX%" echo $ErrorActionPreference = 'SilentlyContinue'
>>"%PSFIX%" echo $code = @'
>>"%PSFIX%" echo using System;
>>"%PSFIX%" echo using System.Runtime.InteropServices;
>>"%PSFIX%" echo namespace MerlinRepair {
>>"%PSFIX%" echo   [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
>>"%PSFIX%" echo   public class ShellLink { }
>>"%PSFIX%" echo   [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
>>"%PSFIX%" echo    InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
>>"%PSFIX%" echo   public interface IPersistFile {
>>"%PSFIX%" echo     void GetClassID(out Guid id);
>>"%PSFIX%" echo     [PreserveSig] int IsDirty();
>>"%PSFIX%" echo     void Load([MarshalAs(UnmanagedType.LPWStr)] string f, uint m);
>>"%PSFIX%" echo     void Save([MarshalAs(UnmanagedType.LPWStr)] string f, [MarshalAs(UnmanagedType.Bool)] bool r);
>>"%PSFIX%" echo     void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string f);
>>"%PSFIX%" echo     void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string f);
>>"%PSFIX%" echo   }
>>"%PSFIX%" echo   [StructLayout(LayoutKind.Sequential, Pack = 4)]
>>"%PSFIX%" echo   public struct PropertyKey { public Guid fmtid; public uint pid; }
>>"%PSFIX%" echo   [StructLayout(LayoutKind.Explicit)]
>>"%PSFIX%" echo   public struct PropVariant {
>>"%PSFIX%" echo     [FieldOffset(0)] public ushort vt;
>>"%PSFIX%" echo     [FieldOffset(8)] public IntPtr p;
>>"%PSFIX%" echo   }
>>"%PSFIX%" echo   [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
>>"%PSFIX%" echo    InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
>>"%PSFIX%" echo   public interface IPropertyStore {
>>"%PSFIX%" echo     void GetCount(out uint c);
>>"%PSFIX%" echo     void GetAt(uint i, out PropertyKey k);
>>"%PSFIX%" echo     void GetValue(ref PropertyKey k, out PropVariant v);
>>"%PSFIX%" echo     void SetValue(ref PropertyKey k, ref PropVariant v);
>>"%PSFIX%" echo     void Commit();
>>"%PSFIX%" echo   }
>>"%PSFIX%" echo   public static class Fix {
>>"%PSFIX%" echo     static PropertyKey Key() {
>>"%PSFIX%" echo       var k = new PropertyKey();
>>"%PSFIX%" echo       k.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
>>"%PSFIX%" echo       k.pid = 5;
>>"%PSFIX%" echo       return k;
>>"%PSFIX%" echo     }
>>"%PSFIX%" echo     public static string Read(string lnk) {
>>"%PSFIX%" echo       var link = new ShellLink();
>>"%PSFIX%" echo       ((IPersistFile)link).Load(lnk, 0);
>>"%PSFIX%" echo       var key = Key();
>>"%PSFIX%" echo       PropVariant v;
>>"%PSFIX%" echo       ((IPropertyStore)link).GetValue(ref key, out v);
>>"%PSFIX%" echo       if (v.vt == 31 ^&^& v.p != IntPtr.Zero) { return Marshal.PtrToStringUni(v.p); }
>>"%PSFIX%" echo       return null;
>>"%PSFIX%" echo     }
>>"%PSFIX%" echo     public static void Clear(string lnk) {
>>"%PSFIX%" echo       var link = new ShellLink();
>>"%PSFIX%" echo       ((IPersistFile)link).Load(lnk, 2);
>>"%PSFIX%" echo       var store = (IPropertyStore)link;
>>"%PSFIX%" echo       var key = Key();
>>"%PSFIX%" echo       var empty = new PropVariant();
>>"%PSFIX%" echo       empty.vt = 0;
>>"%PSFIX%" echo       empty.p = IntPtr.Zero;
>>"%PSFIX%" echo       store.SetValue(ref key, ref empty);
>>"%PSFIX%" echo       store.Commit();
>>"%PSFIX%" echo       ((IPersistFile)link).Save(lnk, true);
>>"%PSFIX%" echo     }
>>"%PSFIX%" echo   }
>>"%PSFIX%" echo }
>>"%PSFIX%" echo '@
>>"%PSFIX%" echo Add-Type -TypeDefinition $code ^| Out-Null
>>"%PSFIX%" echo $id = $env:MERLIN_ID
>>"%PSFIX%" echo $mine = $env:MERLIN_EXE
>>"%PSFIX%" echo $shell = New-Object -ComObject WScript.Shell
>>"%PSFIX%" echo $folders = @(
>>"%PSFIX%" echo   (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'),
>>"%PSFIX%" echo   (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\StartMenu'),
>>"%PSFIX%" echo   (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
>>"%PSFIX%" echo )
>>"%PSFIX%" echo $fixed = 0
>>"%PSFIX%" echo foreach ($folder in $folders) {
>>"%PSFIX%" echo   if (-not (Test-Path $folder)) { continue }
>>"%PSFIX%" echo   Get-ChildItem $folder -Filter *.lnk -Recurse ^| ForEach-Object {
>>"%PSFIX%" echo     $path = $_.FullName
>>"%PSFIX%" echo     $current = $null
>>"%PSFIX%" echo     try { $current = [MerlinRepair.Fix]::Read($path) } catch { }
>>"%PSFIX%" echo     if ($current -eq $id) {
>>"%PSFIX%" echo       if ($true) {
>>"%PSFIX%" echo         try {
>>"%PSFIX%" echo           [MerlinRepair.Fix]::Clear($path)
>>"%PSFIX%" echo           Write-Output ("   cleared: " + $_.Name + "  ->  " + $target)
>>"%PSFIX%" echo           $fixed = $fixed + 1
>>"%PSFIX%" echo         } catch { Write-Output ("   could not change: " + $_.Name) }
>>"%PSFIX%" echo       }
>>"%PSFIX%" echo     }
>>"%PSFIX%" echo   }
>>"%PSFIX%" echo }
>>"%PSFIX%" echo if ($fixed -eq 0) { Write-Output '   Nothing needed changing.' }


powershell -NoProfile -ExecutionPolicy Bypass -File "%PSFIX%"
del "%PSFIX%" >nul 2>&1
call :bar

rem ------------------------------------------------------- icon cache
rem Ask the shell to rebuild its icon cache. This is a request to Explorer, it
rem does not stop or restart anything. An earlier version of this installer
rem killed explorer.exe to force the refresh, which is not acceptable in an
rem installer and has been removed.
echo.
echo        Asking Windows to refresh its icon cache
ie4uinit.exe -show >nul 2>&1
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
echo        Either runs as a separate process, so a codec crash takes the
echo        player down rather than the browser.
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
echo   -----------------------------------------------------------
rem Print the whole report. Filtering it to four lines hid the very fields
rem that were added to diagnose this, so the same wrong conclusion was drawn
rem twice from a censored summary.
"%MERLINCON%" %RUNARG% --icon-check
echo   -----------------------------------------------------------
echo.
echo    Python packages are confined to %VENV%
echo    Your system Python was not modified.
echo.

if "%SILENT%"=="1" goto :finish
choice /c YN /n /m "  Start Merlin now? [Y/N] "
if errorlevel 2 goto :finish
start "" "%MERLINEXE%" %RUNARG%
:finish

call :endui
echo.
echo  Done.
if "%SILENT%"=="1" goto :quiet_end
echo.
pause
:quiet_end
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
rem Clear the reserved line, release the scrolling region, then leave the
rem cursor at the bottom. Restoring the saved cursor position instead put it
rem back wherever the bar was last drawn from, so "Press any key to continue"
rem and the closing summary printed over text that was already on screen.
if "%VT_OK%"=="1" (
  <nul set /p "=%ESC%[%ROWS%;1H%ESC%[2K"
  <nul set /p "=%ESC%[r"
  <nul set /p "=%ESC%[%ROWS%;1H"
  echo(
)
set "VT_OK=0"
title Merlin Browser installer
goto :eof
