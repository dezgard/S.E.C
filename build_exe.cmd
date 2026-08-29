@echo off
setlocal
title Build Star Empire Companion
cd /d "%~dp0"

where pyinstaller >nul 2>&1
if errorlevel 1 (
  echo PyInstaller is not available on PATH.
  echo Install or restore PyInstaller, then run this file again.
  pause
  exit /b 1
)

set "BUILD_ROOT=%TEMP%\StarEmpireCompanion-PyInstaller"
set "BUILD_OUTPUT=%~dp0builds\current"
set "RELEASE_OUTPUT=%~dp0releases\current"
set "BACKUP_ROOT=%~dp0backups"

if not exist "%BUILD_OUTPUT%" mkdir "%BUILD_OUTPUT%"
if not exist "%RELEASE_OUTPUT%" mkdir "%RELEASE_OUTPUT%"
if not exist "%BACKUP_ROOT%" mkdir "%BACKUP_ROOT%"

if exist "%RELEASE_OUTPUT%\StarEmpireCompanion.exe" (
  echo Backing up the current executable...
  powershell.exe -NoProfile -Command "$stamp=Get-Date -Format 'yyyyMMdd.HHmmss'; $old=(Get-Item -LiteralPath '%RELEASE_OUTPUT%\StarEmpireCompanion.exe').VersionInfo.FileVersion; if([string]::IsNullOrWhiteSpace($old)){$old='unknown'}; $backup=Join-Path '%BACKUP_ROOT%' ('StarEmpireCompanion.' + $stamp + '.V' + $old); New-Item -ItemType Directory -Path $backup -Force | Out-Null; Copy-Item -LiteralPath '%RELEASE_OUTPUT%\StarEmpireCompanion.exe' -Destination $backup; $checksum='%RELEASE_OUTPUT%\StarEmpireCompanion.exe.sha256'; if(Test-Path -LiteralPath $checksum){Copy-Item -LiteralPath $checksum -Destination $backup}"
  if errorlevel 1 (
    echo Could not back up the existing executable. Build stopped.
    pause
    exit /b 1
  )
)

echo Building the single-file Windows application...
pyinstaller --noconfirm --clean --log-level=WARN --distpath="%BUILD_OUTPUT%" --workpath="%BUILD_ROOT%\work" StarEmpireCompanion.spec

if errorlevel 1 (
  echo.
  echo Build failed. Copy the output above when reporting the problem.
  pause
  exit /b 1
)

copy /y "%BUILD_OUTPUT%\StarEmpireCompanion.exe" "%RELEASE_OUTPUT%\StarEmpireCompanion.exe" >nul
if errorlevel 1 (
  echo Could not copy the completed build into releases\current. Build stopped.
  pause
  exit /b 1
)

powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $file='%RELEASE_OUTPUT%\StarEmpireCompanion.exe'; $algorithm=[Security.Cryptography.SHA256]::Create(); $stream=[IO.File]::OpenRead($file); try{$hash=([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','')}finally{$stream.Dispose(); $algorithm.Dispose()}; [IO.File]::WriteAllText('%RELEASE_OUTPUT%\StarEmpireCompanion.exe.sha256', ($hash + ' *StarEmpireCompanion.exe'), [Text.Encoding]::ASCII)"
if errorlevel 1 (
  echo Could not create the release checksum. Build stopped.
  pause
  exit /b 1
)

echo.
echo Built successfully:
echo %RELEASE_OUTPUT%\StarEmpireCompanion.exe
echo %RELEASE_OUTPUT%\StarEmpireCompanion.exe.sha256
pause
