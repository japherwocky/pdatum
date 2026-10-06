@echo off
rem pdatum installer for the Windows Command Prompt.
rem
rem   curl -fsSL https://pdatum.pearachute.com/install.cmd -o install.cmd && install.cmd && del install.cmd
rem
rem CMD cannot run a script from a pipe, hence the download-run-delete. The
rem work is done by install.ps1, through the PowerShell every Windows ships
rem with; this only hands over to it.

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://raw.githubusercontent.com/japherwocky/pdatum/main/install/install.ps1' | iex"
if errorlevel 1 exit /b 1
echo.
echo Open a new Command Prompt to use pdatum: this one started before it was installed.
endlocal
