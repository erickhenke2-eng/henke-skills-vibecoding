# ==============================================================================
# Instalador & Sincronizador Automático da Suíte Henke (Windows PowerShell)
# Espelha as 47 skills em ~/.agents/skills, ~/.gemini/config/skills e ~/.claude/skills
# ==============================================================================

$ErrorActionPreference = "Stop"

$userProfile = [System.Environment]::GetFolderPath('UserProfile')
$agentsBase = Join-Path $userProfile ".agents\skills"
$geminiBase = Join-Path $userProfile ".gemini\config\skills"
$claudeBase = Join-Path $userProfile ".claude\skills"

$repoRoot = $PSScriptRoot

Write-Host "Instalando Suíte Henke de Skills..." -ForegroundColor Cyan

# Garante que as pastas de destino existam
$targets = @($agentsBase, $geminiBase, $claudeBase)
foreach ($target in $targets) {
    if (-not (Test-Path $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
}

$skillDirs = Get-ChildItem -Path $repoRoot -Directory | Where-Object { $_.Name -like "henke-*" }
$count = 0

foreach ($dir in $skillDirs) {
    $skillFile = Join-Path $dir.FullName "SKILL.md"
    if (Test-Path $skillFile) {
        $slug = $dir.Name
        
        # 1. Copia para ~/.agents/skills (canônico)
        $cDir = Join-Path $agentsBase $slug
        if (-not (Test-Path $cDir)) { New-Item -ItemType Directory -Path $cDir -Force | Out-Null }
        $cFile = Join-Path $cDir "SKILL.md"
        Copy-Item -Path $skillFile -Destination $cFile -Force

        # 2. Espelha via Hardlink ou Cópia para Gemini e Claude
        foreach ($targetBase in @($geminiBase, $claudeBase)) {
            $tDir = Join-Path $targetBase $slug
            if (-not (Test-Path $tDir)) { New-Item -ItemType Directory -Path $tDir -Force | Out-Null }
            $tFile = Join-Path $tDir "SKILL.md"
            if (Test-Path $tFile) { Remove-Item $tFile -Force }
            
            try {
                fsutil hardlink create $tFile $cFile | Out-Null
            } catch {
                Copy-Item -Path $cFile -Destination $tFile -Force
            }
        }
        $count++
        Write-Host "  -> [$count/47] Sincronizada: $slug" -ForegroundColor Green
    }
}

Write-Host "`nSucesso! $count Macro-Skills Henke ativas no Antigravity, Claude Code e Codex." -ForegroundColor Yellow
