@echo off
rem henke.cmd - wrapper Windows para a CLI global da Suite Henke (v4.0).
rem Resolve henke\henke_cli.py ao lado do bin\; no wheel os scripts vivem
rem em site-packages - cai no modulo instalado.
setlocal
set "HENKE_CLI=%~dp0..\henke\henke_cli.py"

rem R2-PT-06: -I (isolado) no lancador - ignora PYTHONPATH e user site,
rem entao sitecustomize.py do repo auditado nunca executa no processo.
if exist "%HENKE_CLI%" (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -I "%HENKE_CLI%" %*
    exit /b %ERRORLEVEL%
  )
  py -3 -I "%HENKE_CLI%" %*
  exit /b %ERRORLEVEL%
)

rem Instalacao via wheel/pipx: <prefix>\henke_data\bin\henke.cmd com
rem henke\ em site-packages - delega ao pacote importavel.
where python >nul 2>nul
if not errorlevel 1 (
  python -I -m henke.henke_cli %*
  exit /b %ERRORLEVEL%
)
py -3 -I -m henke.henke_cli %*
exit /b %ERRORLEVEL%
