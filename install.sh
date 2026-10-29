#!/usr/bin/env bash
# Instalador da Suíte Henke (Linux / macOS) - PROTOCOLO v3.1.1
# Valida a integridade estrita do pacote antes de alterar qualquer arquivo e espelha
# as 49 skills em ~/.agents/skills, ~/.gemini/config/skills, ~/.gemini/skills e ~/.claude/skills.
set -euo pipefail

dry_run=0
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    dry_run=1
  fi
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Pré-validação estrita do repositório (Portão Mecânico Antes da Escrita)
printf 'Executando pré-validação estrita da Suíte Henke...\n'

proto_source="$repo_root/PROTOCOLO.md"
if [[ ! -f "$proto_source" ]] || [[ $(wc -c < "$proto_source") -lt 200 ]]; then
  printf 'ERRO CRÍTICO: PROTOCOLO.md canônico ausente ou vazio na raiz. Nenhuma alteração realizada.\n' >&2
  exit 1
fi

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  printf 'ERRO CRÍTICO: Python não encontrado no ambiente para validação prévia. Instalação abortada.\n' >&2
  exit 1
fi

if ! "$PYTHON_BIN" "$repo_root/scripts/validate_skills.py"; then
  printf 'ERRO CRÍTICO: Validação da suíte falhou. Instalação abortada antes de alterar qualquer destino.\n' >&2
  exit 1
fi

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

if [[ ${#skills[@]} -ne 49 ]]; then
  printf 'ERRO CRÍTICO: inventário inesperado (%s skills; esperadas: 49). Instalação abortada.\n' "${#skills[@]}" >&2
  exit 1
fi

if [[ $dry_run -eq 1 ]]; then
  printf '\n[DRY-RUN] Pré-validação concluída com sucesso!\n'
  printf '[DRY-RUN] Seriam espelhadas %s skills + PROTOCOLO.md nos seguintes destinos:\n' "${#skills[@]}"
  for target in "${targets[@]}"; do
    printf '  -> %s\n' "$target"
  done
  exit 0
fi

# 2. Instalação nos diretórios de destino
for target in "${targets[@]}"; do
  mkdir -p "$target"
done

# Copia PROTOCOLO.md
for target in "${targets[@]}"; do
  cp "$proto_source" "$target/PROTOCOLO.md"
done
printf 'PROTOCOLO.md canônico copiado para todos os destinos.\n'

for skill_dir in "${skills[@]}"; do
  slug="$(basename "$skill_dir")"
  for target in "${targets[@]}"; do
    mkdir -p "$target/$slug"
    cp "$skill_dir/SKILL.md" "$target/$slug/SKILL.md"
  done
  printf 'Instalada: %s\n' "$slug"
done

printf '\nSucesso: %s skills + PROTOCOLO.md validados e instalados com sucesso em todos os destinos.\n' "${#skills[@]}"
