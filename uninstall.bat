@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem ===========================================================================
rem  Merlin Browser - Windows uninstaller
rem
rem  This script normally lives inside the folder it has to delete, and Windows
rem  will not remove a directory that holds a running batch file: cmd keeps a
rem  handle on it, which surfaces as "Access is denied". So the first thing it
rem  does is copy itself to %TEMP% and re-run from there.
rem ===========================================================================

title Merlin Browser uninstaller

rem Unattended mode, for the graphical uninstaller. Each prompt takes its
rem answer from an environment variable instead of the keyboard.
set "SILENT=0"
if /i "%~1"=="--yes" set "SILENT=1"
if "%MERLIN_SILENT%"=="1" set "SILENT=1"

set "TARGET=%LOCALAPPDATA%\Programs\Merlin"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "PROFILE=%APPDATA%\Merlin"
set "LOCALDATA=%LOCALAPPDATA%\Merlin"
set "RELAUNCHED=%~1"

if not "%RELAUNCHED%"=="--relaunched" (
  copy /y "%~f0" "%TEMP%\merlin-uninstall.bat" >nul 2>&1
  if errorlevel 1 (
    echo Could not copy the uninstaller to a temporary folder.
    pause
    exit /b 1
  )
  start "" /wait "%TEMP%\merlin-uninstall.bat" --relaunched
  exit /b 0
)

echo.
echo   Uninstalling Merlin Browser
echo.

rem A running Merlin holds its DLLs open, which blocks the delete. Only
rem Merlin.exe is considered: an earlier version matched pythonw.exe, which
rem would have closed any other Python program you had open.
tasklist /fi "IMAGENAME eq Merlin.exe" 2>nul | find /i "Merlin.exe" >nul
if not errorlevel 1 (
  echo   Merlin is running. It has to close before its files can be removed.
  if "%SILENT%"=="1" (set "CLOSEIT=1") else (
    choice /c YN /n /m "   Close Merlin now? [Y/N] "
    if errorlevel 2 (set "CLOSEIT=0") else (set "CLOSEIT=1")
  )
  if "!CLOSEIT!"=="1" (
    taskkill /im Merlin.exe >nul 2>&1
    timeout /t 2 >nul
    tasklist /fi "IMAGENAME eq Merlin.exe" 2>nul | find /i "Merlin.exe" >nul
    if not errorlevel 1 (
      echo   Merlin did not close. Close it yourself and run this again.
      pause
      exit /b 1
    )
  )
)

del "%STARTMENU%\Merlin Browser.lnk" >nul 2>&1
del "%STARTMENU%\Merlin Browser (Frameless).lnk" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "Remove-Item ([Environment]::GetFolderPath('Desktop')+'\Merlin Browser.lnk') -ErrorAction SilentlyContinue" >nul 2>&1
rem Pinned taskbar entries are deliberately left alone. Matching on a path
rem fragment risked deleting a pin belonging to another application, and an
rem uninstaller has no business removing shortcuts it did not create. Unpin
rem Merlin by hand if you had it pinned.
echo   Start Menu shortcuts removed.

set "MERLIN_TARGET=%TARGET%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$p=[Environment]::GetEnvironmentVariable('Path','User'); $n=(($p -split ';') ^| Where-Object { $_ -and $_ -ne $env:MERLIN_TARGET }) -join ';'; [Environment]::SetEnvironmentVariable('Path',$n,'User')" >nul 2>&1
echo   PATH entry removed.

echo.
echo   Bookmarks, history and settings live in:
echo       %PROFILE%
rem The answer is decided once, into DELPROFILE, and then acted on. Testing
rem errorlevel further down would pick up whatever ran last instead.
set "DELPROFILE=0"
if "%SILENT%"=="1" (
  if "%MERLIN_KEEP_PROFILE%"=="0" set "DELPROFILE=1"
) else (
  choice /c YN /n /m "   Delete those as well? [Y/N] "
  if not errorlevel 2 set "DELPROFILE=1"
)
if not "!DELPROFILE!"=="1" goto :keepprofile
rmdir /s /q "%PROFILE%"   >nul 2>&1
rmdir /s /q "%LOCALDATA%" >nul 2>&1
echo   Profile deleted.
goto :removeapp

:keepprofile
rmdir /s /q "%LOCALDATA%\cache" >nul 2>&1
echo   Profile kept.

:removeapp
echo.
rmdir /s /q "%TARGET%" >nul 2>&1
if exist "%TARGET%" (
  timeout /t 2 >nul
  rmdir /s /q "%TARGET%" >nul 2>&1
)
if exist "%TARGET%" (
  echo   [!] Some files could not be removed:
  echo         %TARGET%
  echo       That usually means Merlin is still running. Close it and delete
  echo       the folder by hand, or run this uninstaller again.
) else (
  echo   Merlin removed.
)

echo.
echo   PyQt6 lived inside Merlin's own virtualenv and went with it. Your
echo   system Python was never modified.
echo.
pause
endlocal
