@echo off
setlocal enabledelayedexpansion

rem ===================================================================
rem  SoothingSpaces session launcher. Mark J. Kuebel. MIT.
rem
rem    start_session.bat              run an EMDR safe-space session
rem    start_session.bat --preview    build a mix, send nothing (test)
rem
rem  The bridge must be running: start it first with run.bat and leave
rem  its window open. No model server? pass --no-model to build the
rem  scent mix from palette rules instead of asking the LLM. A safe
rem  space on file is offered for recall first; --skip-recall goes
rem  straight to intake. Multi-frame option: --keyframes N (N>1).
rem ===================================================================

cd /d "%~dp0"

set "PYTHON="
where py >nul 2>&1 && set "PYTHON=py"
if not defined PYTHON where python >nul 2>&1 && set "PYTHON=python"
if not defined PYTHON (
    echo ERROR: no Python launcher found. Install Python and try again.
    pause
    exit /b 1
)

echo Starting SoothingSpaces safe-space session...
%PYTHON% session.py %*

echo.
pause
