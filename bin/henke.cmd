@echo off
rem henke.cmd - wrapper Windows para a CLI global da Suite Henke (v4.0).
rem Resolve scripts\henke_cli.py ao lado do bin\; no wheel os scripts vivem
rem em site-packages - cai no modulo instalado.
setlocal
set "HENKE_CLI=%~dp0..\scripts\henke_cli.py"

if exist "%HENKE_CLI%" (
  where python >nul 2>nul
  if not errorlevel 1 (
    python "%HENKE_CLI%" %*
    exit /b %ERRORLEVEL%
  )
  py -3 "%HENKE_CLI%" %*
  exit /b %ERRORLEVEL%
)

rem Instalacao via wheel/pipx: <prefix>\henke_data\bin\henke.cmd com
rem scripts\ em site-packages - delega ao pacote importavel.
where python >nul 2>nul
if not errorlevel 1 (
  python -m scripts.henke_cli %*
  exit /b %ERRORLEVEL%
)
py -3 -m scripts.henke_cli %*
exit /b %ERRORLEVEL%
