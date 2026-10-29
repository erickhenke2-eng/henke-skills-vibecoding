#!/usr/bin/env python3
"""
Validação estrutural e semântica da Suíte Henke (PROTOCOLO v3.1.1).
Endurecido para rejeitar adulterações: YAML inválido, chaves duplicadas,
links quebrados, protocolo vazio ou simulado, e suíte esvaziada.
"""
from pathlib import Path
import re
import sys
import yaml
from yaml.constructor import ConstructorError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

class UniqueKeyLoader(yaml.SafeLoader):
    """YAML Loader que rejeita chaves duplicadas no mesmo mapeamento."""
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConstructorError(f"Chave duplicada detectada no YAML: '{key}'")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

root = Path(__file__).resolve().parents[1]
skills = sorted(p for p in root.glob("henke-*/SKILL.md") if p.is_file())
errors = []

# 1. Validação estrita do PROTOCOLO.md canônico
protocolo = root / "PROTOCOLO.md"
if not protocolo.is_file():
    errors.append("PROTOCOLO.md canônico ausente na raiz do repositório")
else:
    proto_text = protocolo.read_text(encoding="utf-8")
    if len(proto_text.strip()) < 200:
        errors.append("PROTOCOLO.md vazio ou com tamanho insuficiente (< 200 caracteres)")
    # Rejeita texto arbitrário (ex: 101 caracteres 'x')
    required_proto_markers = [
        "# Protocolo Henke",
        "## 1. Modos de Operação",
        "## 6. Evidência",
        "## 10. Portões Mecânicos"
    ]
    for marker in required_proto_markers:
        if marker not in proto_text:
            errors.append(f"PROTOCOLO.md sem estrutura canônica obrigatória: marcador ausente '{marker}'")

# 2. Validação da contagem de skills (49 skills esperadas)
if len(skills) != 49:
    errors.append(f"Inventário divergente: esperadas 49 skills, encontradas {len(skills)}")

names = set()
for path in skills:
    slug = path.parent.name
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(f"{slug}: falha ao ler arquivo ({e})")
        continue

    # Extração de frontmatter delimitado por ---
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", content, flags=re.S)
    if not match:
        errors.append(f"{slug}: frontmatter YAML ausente ou mal delimitado por '---'")
        continue

    frontmatter_raw = match.group(1)
    body = match.group(2)

    # 3. Parser YAML estrito com UniqueKeyLoader
    try:
        data = yaml.load(frontmatter_raw, Loader=UniqueKeyLoader)
    except Exception as e:
        errors.append(f"{slug}: YAML inválido ou chave duplicada no frontmatter ({e})")
        continue

    if not isinstance(data, dict):
        errors.append(f"{slug}: frontmatter YAML não é um mapeamento/dicionário válido")
        continue

    # 4. Validação de campos obrigatórios do frontmatter
    name = data.get("name")
    if not name or not isinstance(name, str) or name.strip() != slug:
        errors.append(f"{slug}: campo 'name' inválido ou divergente do diretório (esperado '{slug}', obtido '{name}')")
    elif name in names:
        errors.append(f"{slug}: nome duplicado no inventário da suíte ('{name}')")
    else:
        names.add(name)

    description = data.get("description")
    if not description or not isinstance(description, str) or len(description.strip()) < 10:
        errors.append(f"{slug}: campo 'description' ausente, vazio ou não textual")

    # 5. Validação de conteúdo operacional (anti-esvaziamento)
    if len(body.strip()) < 200:
        errors.append(f"{slug}: corpo operacional esvaziado ou insuficiente (< 200 caracteres)")

    # 6. Validação de seção de verificação calibrada
    if "## 6." not in body or "- [ ]" not in body:
        errors.append(f"{slug}: seção '## 6.' ou checklist de verificação (- [ ]) ausente")

    # 7. Resolução de links relativos em Markdown (rejeita links quebrados)
    markdown_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body)
    for link_text, link_target in markdown_links:
        link_clean = link_target.strip().split("#")[0]
        if not link_clean or link_clean.startswith(("http://", "https://", "mailto:")):
            continue
        target_path = (path.parent / link_clean).resolve()
        if not target_path.exists():
            errors.append(f"{slug}: link relativo quebrado para '{link_target}'")

# 8. Validação específica do Maestro (henke-skills)
maestro_path = root / "henke-skills" / "SKILL.md"
if maestro_path.is_file():
    maestro_text = maestro_path.read_text(encoding="utf-8")
    # Verifica se a Fase 5 tenta entregar template fixo de sucesso
    if "### Fase 5" in maestro_text:
        fase5 = maestro_text.split("### Fase 5")[1]
        fase5_template = fase5.split("## 4.")[0] if "## 4." in fase5 else fase5
        if "Missão orquestrada com sucesso" in fase5_template:
            errors.append("henke-skills: template de entrega na Fase 5 contém fórmula fixa ufanista ('Missão orquestrada com sucesso')")
    if "O que NÃO foi verificado" not in maestro_text:
        errors.append("henke-skills: template de entrega sem a seção obrigatória 'O que NÃO foi verificado'")

if errors:
    print(f"FALHA NA VALIDAÇÃO ({len(errors)} erros encontrados):")
    for err in errors:
        print(f"  [ERRO] {err}")
    sys.exit(1)

print(f"OK: {len(skills)} skills válidas (YAML estrito, links resolvidos, seções calibradas e PROTOCOLO.md íntegro).")
