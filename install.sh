#!/usr/bin/env bash
# Instalador da Suíte Henke (Linux / macOS) - PROTOCOLO v3.1.1
# Valida o pacote, prepara cópias e restaura arquivos se houver erro de escrita.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" ]]; then
  printf 'Python não encontrado. Instale Python e as dependências de requirements.txt.\n' >&2
  exit 1
fi
exec "$python_bin" "$repo_root/scripts/install_skills.py" "$@"
