@echo off
rem henke.cmd - wrapper Windows para a CLI global da Suite Henke (v4.0).
rem Resolve scripts\henke_cli.py instalado ao lado deste diretorio bin\.
setlocal
set "HENKE_CLI=%~dp0..\scripts\henke_cli.py"

if not exist "%HENKE_CLI%" (
  echo ERRO: henke_cli.py nao encontrado em "%HENKE_CLI%". 1>&2
  exit /b 2
)

where python >nul 2>nul
if not errorlevel 1 (
  python "%HENKE_CLI%" %*
  exit /b %ERRORLEVEL%
)

py -3 "%HENKE_CLI%" %*
exit /b %ERRORLEVEL%
