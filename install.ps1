# Instalador da Suíte Henke (Windows PowerShell)
# Copia SKILL.md sem hardlinks, que podem falhar silenciosamente.
$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$homeDir = [System.Environment]::GetFolderPath('UserProfile')
$targets = @(
    (Join-Path $homeDir ".agents\skills"),
    (Join-Path $homeDir ".gemini\skills"),
    (Join-Path $homeDir ".claude\skills")
)
$skills = @(Get-ChildItem -Path $repoRoot -Directory | Where-Object { $_.Name -like "henke-*" -and (Test-Path (Join-Path $_.FullName "SKILL.md")) })
if ($skills.Count -ne 47) { throw "Inventario inesperado: $($skills.Count) skills. Esperadas: 47. Instalação cancelada." }
foreach ($target in $targets) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
foreach ($dir in $skills) {
    foreach ($target in $targets) {
        $destination = Join-Path $target $dir.Name
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $dir.FullName "SKILL.md") -Destination (Join-Path $destination "SKILL.md") -Force
    }
    Write-Host "Instalada: $($dir.Name)"
}
Write-Host "Sucesso: $($skills.Count) skills copiadas para agents, Gemini e Claude."
