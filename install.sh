#!/usr/bin/env bash
# Instalador da Suíte Henke (Linux / macOS)
# Espelha as 47 skills em ~/.agents/skills, ~/.gemini/config/skills, ~/.gemini/skills e ~/.claude/skills
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
targets=(
  "$HOME/.agents/skills"
  "$HOME/.gemini/config/skills"
  "$HOME/.gemini/skills"
  "$HOME/.claude/skills"
)

shopt -s nullglob
skills=()
for skill_dir in "$repo_root"/henke-*/; do
  [[ -f "${skill_dir}SKILL.md" ]] && skills+=("$skill_dir")
done

if [[ ${#skills[@]} -ne 47 ]]; then
  printf 'Erro: inventário inesperado (%s skills; esperadas: 47). Instalação cancelada.\n' "${#skills[@]}" >&2
  exit 1
fi

for target in "${targets[@]}"; do
  mkdir -p "$target"
done

for skill_dir in "${skills[@]}"; do
  slug="$(basename "$skill_dir")"
  for target in "${targets[@]}"; do
    mkdir -p "$target/$slug"
    cp "$skill_dir/SKILL.md" "$target/$slug/SKILL.md"
  done
  printf 'Instalada: %s\n' "$slug"
done

printf '\nSucesso: %s skills copiadas para agents, Antigravity/Gemini e Claude.\n' "${#skills[@]}"
