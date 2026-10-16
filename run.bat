@echo off
rem Windows 10/11 launcher: runs run.sh with Git Bash (installed with Git for Windows).
setlocal
set "BASH="
for %%P in ("%ProgramFiles%\Git\bin\bash.exe" "%ProgramFiles(x86)%\Git\bin\bash.exe" "%LocalAppData%\Programs\Git\bin\bash.exe") do (
    if exist %%P if not defined BASH set "BASH=%%~P"
)
if not defined BASH (
    where winget >nul 2>&1 && (
        echo Installing Git for Windows, which provides bash...
        winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
        for %%P in ("%ProgramFiles%\Git\bin\bash.exe") do if exist %%P set "BASH=%%~P"
    )
)
if not defined BASH (
    echo Git Bash was not found. Install Git for Windows from https://git-scm.com and run this again.
    pause
    exit /b 1
)
"%BASH%" "%~dp0run.sh" %*
if errorlevel 1 pause
