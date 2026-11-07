@echo off
setlocal
for %%T in (setup services demo verify clean) do if /I "%~1"=="%%T" set "target=%%T"
if not defined target (
  echo Usage: make.bat setup^|services^|demo^|verify^|clean
  exit /b 2
)
set "bash=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%bash%" set "bash=bash.exe"
pushd "%~dp0" || exit /b 1
"%bash%" ./start_demo.sh --%target%
set "result=%ERRORLEVEL%"
popd
exit /b %result%
