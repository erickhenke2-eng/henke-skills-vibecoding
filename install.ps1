<#
.SYNOPSIS
Instalador da Suíte Henke (Windows PowerShell) - PROTOCOLO v3.1.1
.DESCRIPTION
Valida o pacote, prepara cópias e restaura arquivos se houver erro de escrita.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$UserRoot = [System.Environment]::GetFolderPath('UserProfile')
)
$ErrorActionPreference = "Stop"
$installerArgs = @((Join-Path $PSScriptRoot "scripts/install_skills.py"), "--user-root", $UserRoot)
if ($DryRun) { $installerArgs += "--dry-run" }
& python @installerArgs
exit $LASTEXITCODE
