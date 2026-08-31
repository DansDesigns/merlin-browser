@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  Merlin Browser - Windows uninstaller
rem
rem  This script normally lives inside the folder it has to delete, and Windows
rem  will not remove a directory that holds a running batch file: cmd keeps a
rem  handle on it, which surfaces as "Access is denied". So the first thing it
rem  does is copy itself to %TEMP% and re-run from there.
rem ===========================================================================

title Merlin Browser uninstaller

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

rem A running Merlin holds its DLLs open, which also blocks the delete.
tasklist /fi "IMAGENAME eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if not errorlevel 1 (
  echo   Merlin appears to be running. Close it before continuing.
  choice /c YN /n /m "   Close it for me? [Y/N] "
  if not errorlevel 2 (
    taskkill /f /im pythonw.exe >nul 2>&1
    timeout /t 2 >nul
  )
)

del "%STARTMENU%\Merlin Browser.lnk" >nul 2>&1
del "%STARTMENU%\Merlin Browser (Frameless).lnk" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "Remove-Item ([Environment]::GetFolderPath('Desktop')+'\Merlin Browser.lnk') -ErrorAction SilentlyContinue" >nul 2>&1
echo   Shortcuts removed.

set "MERLIN_TARGET=%TARGET%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$p=[Environment]::GetEnvironmentVariable('Path','User'); $n=(($p -split ';') ^| Where-Object { $_ -and $_ -ne $env:MERLIN_TARGET }) -join ';'; [Environment]::SetEnvironmentVariable('Path',$n,'User')" >nul 2>&1
echo   PATH entry removed.

echo.
echo   Bookmarks, history and settings live in:
echo       %PROFILE%
choice /c YN /n /m "   Delete those as well? [Y/N] "
if errorlevel 2 goto :keepprofile
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
