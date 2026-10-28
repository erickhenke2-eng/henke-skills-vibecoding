# Instalador da Suíte Henke (Windows PowerShell)
# Espelha as 47 skills em ~/.agents/skills, ~/.gemini/config/skills, ~/.gemini/skills e ~/.claude/skills
$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$homeDir = [System.Environment]::GetFolderPath('UserProfile')

$targets = @(
    (Join-Path $homeDir ".agents\skills"),
    (Join-Path $homeDir ".gemini\config\skills"),
    (Join-Path $homeDir ".gemini\skills"),
    (Join-Path $homeDir ".claude\skills")
)

$skills = @(Get-ChildItem -Path $repoRoot -Directory | Where-Object { $_.Name -like "henke-*" -and (Test-Path (Join-Path $_.FullName "SKILL.md")) })
if ($skills.Count -ne 47) { throw "Inventario inesperado: $($skills.Count) skills. Esperadas: 47. Instalação cancelada." }

foreach ($target in $targets) {
    if (-not (Test-Path $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
}

# 1. Copia PROTOCOLO.md canônico para cada diretório de agentes
$protoSource = Join-Path $repoRoot "PROTOCOLO.md"
if (Test-Path $protoSource) {
    foreach ($target in $targets) {
        Copy-Item -LiteralPath $protoSource -Destination (Join-Path $target "PROTOCOLO.md") -Force
    }
    Write-Host "Copiado PROTOCOLO.md canônico para todas as bases de skills." -ForegroundColor Cyan
}

# 2. Copia as 47 skills
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

Write-Host "`nSucesso: $($skills.Count) skills + PROTOCOLO.md copiados para agents, Antigravity/Gemini e Claude." -ForegroundColor Yellow
