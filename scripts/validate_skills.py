#!/usr/bin/env python3
"""
Validação estrutural da Suíte Henke (PROTOCOLO v3.1.1).
Endurecido para rejeitar adulterações: YAML inválido, chaves duplicadas,
links quebrados e ausência de marcadores mínimos. Não avalia semântica.
"""
from pathlib import Path
import re
import sys


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import yaml
    from yaml.constructor import ConstructorError
except ImportError:
    sys.exit("Dependência ausente. Execute: python -m pip install -r requirements.txt")

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

def strip_code(text: str) -> str:
    """Remove fenced code blocks e inline code antes da análise de links/seções.

    Conteúdo dentro de ```...``` / ~~~...~~~ ou `...` é documentação exemplificada,
    não markdown operacional: links ali não devem ser resolvidos fisicamente (F1).
    """
    out = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if not in_fence:
                in_fence = True
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            continue
        if not in_fence:
            out.append(line)
    # Inline code spans: `...`, ``...`` etc. (fechamento com o mesmo nº de crases)
    return re.sub(r"(`+)[^`\n]*?\1", "", "".join(out))


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

    # Corpo sem código: exemplos em fences não contam como estrutura nem geram links.
    body_nocode = strip_code(body)

    # 6. Estrutura mínima anti-esvaziamento: seções canônicas ## 1. a ## 6. (F5)
    for n in range(1, 7):
        if not re.search(rf"^## {n}\.", body_nocode, flags=re.M):
            errors.append(f"{slug}: seção operacional canônica '## {n}.' ausente")
            break
    if "- [ ]" not in body_nocode:
        errors.append(f"{slug}: checklist de verificação (- [ ]) ausente")

    # 7. Vínculo canônico: seção ## 6. deve conter link markdown para o PROTOCOLO (F2)
    section6 = re.search(r"^## 6\..*$", body_nocode, flags=re.M)
    if section6:
        section6_text = body_nocode[section6.start():]
        proto_resolved = protocolo.resolve()
        has_proto_link = any(
            (path.parent / t.strip().split("#")[0]).resolve() == proto_resolved
            for _, t in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", section6_text)
            if t.strip().split("#")[0] and not t.strip().startswith(("http://", "https://"))
        )
        if not has_proto_link:
            errors.append(f"{slug}: seção '## 6.' sem link markdown resolvível para '../PROTOCOLO.md'")

    # 8. Resolução de links relativos em Markdown (rejeita links quebrados; F1: ignora código)
    markdown_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body_nocode)
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

print(f"OK: {len(skills)} skills com estrutura válida (YAML, links locais e marcadores). Semântica e execução não verificadas.")
