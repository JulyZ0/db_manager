@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  Database Instance Manager - Windows one-click launcher
rem
rem  IMPORTANT: this file must stay PURE ASCII.
rem  cmd.exe parses a .bat file using the system code page *before*
rem  chcp takes effect, so any Chinese text here turns into mojibake
rem  and gets executed as a command. All user-facing Chinese messages
rem  are printed by launcher.py instead.
rem ============================================================

rem Always run from the script's own directory
cd /d "%~dp0"

title Database Instance Manager

rem UTF-8 for the Python child process output
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "launcher.py" goto :no_launcher

rem ---------- Locate a usable Python ----------
rem Prefer the "py" launcher (it picks 3.14+ correctly), fall back to python.
rem No parenthesised blocks here on purpose: %errorlevel% inside a block is
rem expanded at parse time and would report the value from before the block.

set "PYEXE="

where py >nul 2>nul
if errorlevel 1 goto :try_python

py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto :try_python

set "PYEXE=py -3"
goto :run

:try_python
where python >nul 2>nul
if errorlevel 1 goto :no_python

python -c "import sys" >nul 2>nul
if errorlevel 1 goto :no_python

set "PYEXE=python"
goto :run

:no_launcher
echo.
echo [ERROR] launcher.py not found in this folder.
echo.
echo   Please make sure you extracted the whole project,
echo   then double-click start.bat again.
echo.
pause
exit /b 1

:no_python
echo.
echo [ERROR] Python was not found on this computer.
echo.
echo   This program needs Python 3.14 or newer.
echo.
echo   1. Download it from:
echo        https://www.python.org/downloads/
echo   2. During installation, tick the box
echo        "Add Python to PATH"
echo   3. Restart your computer, then double-click start.bat again.
echo.
pause
exit /b 1

:run
rem ---------- Hand over to launcher.py ----------
%PYEXE% launcher.py %*
set "EXITCODE=%errorlevel%"

if "%EXITCODE%"=="0" goto :done

echo.
echo Startup failed with exit code %EXITCODE%.
echo You can run diagnostics with:
echo     python launcher.py --check
echo.
pause

:done
endlocal & exit /b %EXITCODE%
