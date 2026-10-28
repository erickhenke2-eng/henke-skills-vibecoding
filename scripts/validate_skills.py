#!/usr/bin/env python3
"""Validação estrutural da Suíte Henke (sem dependências externas)."""
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
skills = sorted(p for p in root.glob("henke-*/SKILL.md") if p.is_file())
errors = []
if len(skills) != 47:
    errors.append(f"Esperadas 47 skills, encontradas {len(skills)}")
names = set()
for path in skills:
    content = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", content, flags=re.S)
    if not match:
        errors.append(f"{path.parent.name}: frontmatter YAML ausente")
        continue
    name = re.search(r"(?m)^name:\s*(\S+)\s*$", match.group(1))
    if not name or name.group(1) != path.parent.name:
        errors.append(f"{path.parent.name}: nome não corresponde ao diretório")
    if name and name.group(1) in names:
        errors.append(f"{path.parent.name}: nome duplicado")
    if name: names.add(name.group(1))
    if not re.search(r"(?m)^description:", match.group(1)):
        errors.append(f"{path.parent.name}: description ausente")
    if "Checklist Meta-Cognitivo Anti-Migué" in content:
        errors.append(f"{path.parent.name}: checklist antigo ainda presente")
if errors:
    print("\n".join("ERRO: " + item for item in errors))
    sys.exit(1)
print(f"OK: {len(skills)} skills com nome, descrição e verificação atualizada.")
