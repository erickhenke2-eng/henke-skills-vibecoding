<#
.SYNOPSIS
Instalador da Suíte Henke (Windows PowerShell) - PROTOCOLO v3.1.1
.DESCRIPTION
Valida a integridade estrita do pacote antes de alterar qualquer arquivo e espelha
as 49 skills em ~/.agents/skills, ~/.gemini/config/skills, ~/.gemini/skills e ~/.claude/skills.
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$homeDir = [System.Environment]::GetFolderPath('UserProfile')

# 1. Pré-validação estrita do repositório (Portão Mecânico Antes da Escrita)
Write-Host "Executando pré-validação estrita da Suíte Henke..." -ForegroundColor Cyan

$protoSource = Join-Path $repoRoot "PROTOCOLO.md"
if (-not (Test-Path $protoSource) -or (Get-Item $protoSource).Length -lt 200) {
    Write-Error "ERRO CRÍTICO: PROTOCOLO.md canônico ausente ou vazio na raiz. Nenhuma alteração realizada."
    exit 1
}

$validatorScript = Join-Path $repoRoot "scripts\validate_skills.py"
& python $validatorScript
if ($LASTEXITCODE -ne 0) {
    Write-Error "ERRO CRÍTICO: Validação da suíte falhou. Instalação abortada antes de alterar qualquer destino."
    exit 1
}

$targets = @(
    (Join-Path $homeDir ".agents\skills"),
    (Join-Path $homeDir ".gemini\config\skills"),
    (Join-Path $homeDir ".gemini\skills"),
    (Join-Path $homeDir ".claude\skills")
)

$skills = @(Get-ChildItem -Path $repoRoot -Directory | Where-Object { $_.Name -like "henke-*" -and (Test-Path (Join-Path $_.FullName "SKILL.md")) })
if ($skills.Count -ne 49) {
    Write-Error "Inventário inesperado: $($skills.Count) skills. Esperadas: 49. Instalação abortada."
    exit 1
}

if ($DryRun) {
    Write-Host "`n[DRY-RUN] Pré-validação concluída com sucesso!" -ForegroundColor Yellow
    Write-Host "[DRY-RUN] Seriam espelhadas $($skills.Count) skills + PROTOCOLO.md nos seguintes destinos:" -ForegroundColor Yellow
    foreach ($target in $targets) {
        Write-Host "  -> $target" -ForegroundColor Gray
    }
    exit 0
}

# 2. Instalação nos diretórios de destino
foreach ($target in $targets) {
    if (-not (Test-Path $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
}

# Copia PROTOCOLO.md
foreach ($target in $targets) {
    Copy-Item -LiteralPath $protoSource -Destination (Join-Path $target "PROTOCOLO.md") -Force
}
Write-Host "PROTOCOLO.md canônico copiado para todos os destinos." -ForegroundColor Cyan

# Copia as 49 skills
foreach ($dir in $skills) {
    foreach ($target in $targets) {
        $destination = Join-Path $target $dir.Name
        if (-not (Test-Path $destination)) {
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
        }
        Copy-Item -LiteralPath (Join-Path $dir.FullName "SKILL.md") -Destination (Join-Path $destination "SKILL.md") -Force
    }
    Write-Host "Instalada: $($dir.Name)" -ForegroundColor Green
}

Write-Host "`nSucesso: $($skills.Count) skills + PROTOCOLO.md validados e instalados com sucesso em todos os destinos." -ForegroundColor Yellow
