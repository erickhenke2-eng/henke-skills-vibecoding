#!/usr/bin/env bash
# ==============================================================================
# Instalador da Suíte Henke de Skills (Linux / macOS)
# ==============================================================================

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_DIR="$HOME/.agents/skills"
CLAUDE_DIR="$HOME/.claude/skills"

echo "Instalando Suíte Henke de Skills..."

mkdir -p "$AGENTS_DIR" "$CLAUDE_DIR"

count=0
for skill_dir in "$REPO_ROOT"/henke-*/; do
    if [ -d "$skill_dir" ]; then
        slug=$(basename "$skill_dir")
        skill_file="$skill_dir/SKILL.md"
        
        if [ -f "$skill_file" ]; then
            # 1. ~/.agents/skills
            mkdir -p "$AGENTS_DIR/$slug"
            cp "$skill_file" "$AGENTS_DIR/$slug/SKILL.md"

            # 2. ~/.claude/skills (symlink ou cópia)
            mkdir -p "$CLAUDE_DIR/$slug"
            ln -sf "$AGENTS_DIR/$slug/SKILL.md" "$CLAUDE_DIR/$slug/SKILL.md" 2>/dev/null || cp "$skill_file" "$CLAUDE_DIR/$slug/SKILL.md"
            
            count=$((count + 1))
            echo "  -> [$count/47] Instalada: $slug"
        fi
    fi
done

echo ""
echo "Sucesso! $count Macro-Skills Henke instaladas em $AGENTS_DIR e $CLAUDE_DIR."
