#!/usr/bin/env python3
"""
henke_router.py — Roteador determinístico de catálogo da Suíte Henke (PROTOCOLO v3.4).

Inspirado na disciplina do `nvidia-skill-finder` (NVIDIA/skills):
- Lê os 53 `SKILL.md` como fonte única da verdade (`description`, `## 2. Ponto de Partida`
  e `when_not` opcional no frontmatter).
- Opera como veto de Modo (`ANALISAR` default sob ambiguidade/pergunta; `requires_human_auth`
  quando detecta vocabulário destrutivo ou `PUBLISH`).
- Usa tabela declarativa de desambiguação negativa (`NEGATIVE_DISAMBIGUATION`).
- Permite `skills: []` (`confidence: "none"`, exit 0) quando o pedido não pertence ao
  catálogo Henke — nunca força pipeline fixo (§8).

Códigos de saída:
  0  roteamento concluído (inclui 0 skills quando fora de escopo)
  2  uso incorreto, prompt vazio, diretório inexistente ou stdout fechado
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# Vocabulário destrutivo e PUBLISH (PROTOCOLO §1) — exige autorização humana explícita
DESTRUCTIVE_SIGNALS = (
    "deleta", "deletar", "deleto", "deletamos", "delete", "exclui", "excluir",
    "excluo", "excluímos", "apago", "apagamos", "drop", "drop table",
    "truncate", "wipe", "wipe database", "destroi", "destruo", "destruir",
    "destruímos", "destroy", "purge",
    "force-push", "push --force", "force push", "push -f", "reset --hard",
    "hard reset", "rm -rf", "apaga o banco", "apagar o banco", "derruba",
)

PUBLISH_SIGNALS = (
    "deploy", "publica", "publicar", "producao", "production",
    "faxina", "limpa o repo", "apaga arquivos", "reversao", "reverte", "rollback",
) + DESTRUCTIVE_SIGNALS

# Sinais interrogativos / somente-inspeção -> travam teto em ANALISAR
INTERROGATIVE_PREFIXES = (
    "como ", "qual ", "quais ", "por que ", "porque ", "o que ", "o que e ", "o que faz ",
    "me explica", "explica ", "onde ", "onde fica", "existe ", "existe algum", "tem como ", "tem como saber",
    "posso ", "podemos ", "devo ", "devemos ", "e seguro ", "e possivel ", "sera que ",
    "how ", "what ", "why ", "where ", "which ", "who ", "explain ", "is there ", "are there ",
    "can i ", "can we ", "could i ", "could we ", "should i ", "should we ", "is it ", "are we ",
    "do we ", "does it ", "how to ", "how do ", "how can ",
)

AUDIT_SIGNALS = (
    "audita", "auditoria", "vibecode", "revisa", "inspeciona", "checa",
    "verifica se", "diagnostica", "investiga", "acha o bug", "onde ta o erro",
    "por que ta falhando", "olha o console", "pentest", "vulnerabilidade",
    "audit", "review", "inspect", "check", "verify", "diagnose", "investigate",
    "find bug", "where is the error", "why is it failing", "vulnerability",
)

PLAN_SIGNALS = (
    "planeja", "plano", "spec", "mvp", "historias", "cartoes de tarefa",
    "fluxo de telas", "jornada do usuario", "ideia de app", "referencias de mercado",
    "pre-mortem", "arquitetura de pastas", "qual stack", "modela o banco",
    "plan", "roadmap", "user stories", "task cards", "user journey", "screen flow",
    "app idea", "market references", "folder architecture", "database model",
)

EXEC_SIGNALS = (
    "implementa", "cria ", "construa", "corrige", "conserta", "edita",
    "adiciona ", "refatora", "desmembra", "renomeia", "integra ", "protege ",
    "prepara o projeto", "inicia o henke", "mudanca", "muda ", "alinha", "ajusta",
    "atualiza", "aplica", "deleta", "deletar", "delete", "exclui", "excluir",
    "drop", "wipe", "truncate", "destroy", "purge",
    "implement", "create", "build", "fix", "patch", "edit", "add", "refactor",
    "split", "rename", "integrate", "protect", "update", "align", "adjust", "apply",
)

# Tabela declarativa de desambiguação negativa (padrão nvidia-skill-finder):
# cada regra define um contexto léxico que NÃO deve acionar certas skills/domínios.
NEGATIVE_DISAMBIGUATION = (
    {
        "id": "http_or_react_route_is_not_skill_router",
        "when_matches": r"\b(rota http|express route|react router|next/router|endpoint get|endpoint post)\b",
        "exclude_skills": ("henke-skills", "henke-plan-radar"),
    },
    {
        "id": "question_about_deploy_is_not_exec_deploy",
        "when_matches": r"^(como|quando|por que|qual)\b.*\b(deploy|publicar|subir pro ar)\b",
        "exclude_skills": ("henke-ops-deploy",),
        "force_mode": "ANALISAR",
    },
    {
        "id": "generic_video_playback_is_not_3d_scene",
        "when_matches": r"\b(video mp4|ffmpeg|legenda srt|cortar video)\b",
        "exclude_skills": ("henke-code-3d-scene",),
    },
    {
        "id": "git_revert_question_is_not_auto_reversao",
        "when_matches": r"^(como|o que faz)\b.*\b(revert|reset|reversao)\b",
        "exclude_skills": ("henke-debug-reversao",),
        "force_mode": "ANALISAR",
    },
    {
        "id": "pentest_is_not_generic_sec_audit",
        "when_matches": r"\b(pentest|red team|teste de invasao|idor|bola|toctou|race condition|prototype pollution|bypass de auth|poc reproduzivel)\b",
        "exclude_skills": ("henke-audit-seguranca", "henke-audit-vibecode"),
    },
    {
        "id": "coverage_audit_is_not_quality_or_sec",
        "when_matches": r"\b(ledger de cobertura|coverage_ledger|auditoria completa com ledger|90% de cobertura na auditoria|o que nao foi verificado)\b",
        "exclude_skills": ("henke-audit-seguranca", "henke-audit-vibecode", "henke-debug-sanidade"),
    },
    {
        "id": "test_coverage_is_not_coverage_audit",
        "when_matches": r"\b(cobertura de testes?|test coverage|coverage do (jest|vitest|pytest)|codecov|lcov)\b",
        "exclude_skills": ("henke-audit-cobertura",),
    },
    {
        "id": "formal_schema_is_not_bridge_fix",
        "when_matches": r"\b(zod|typebox|pydantic|openapi|trpc|json ?schema|avro|protobuf|contrato formal|schemas? compartilhados?)\b",
        "exclude_skills": ("henke-code-ponte",),
    },
    {
        "id": "bridge_fix_is_not_formal_schema",
        "when_matches": r"\b(dados sumiram|campos vieram errados|front e back nao se entend|alinha os tipos)\b",
        "exclude_skills": ("henke-code-contratos",),
    },
    {
        "id": "cloud_design_is_not_deploy_action",
        "when_matches": r"\b(arquitetura|desenha|blinda|hardening|menor privilegio|least privilege|iam|oidc|bucket)\b.*\b(aws|gcp|google cloud|cloudflare|nuvem|infra|s3|gcs|r2)\b",
        "exclude_skills": ("henke-ops-deploy",),
    },
    {
        "id": "env_file_is_not_cloud_arch",
        "when_matches": r"\b(\.env\b|variaveis de ambiente locais|dotenv|env\.example)\b",
        "exclude_skills": ("henke-arch-cloud",),
    },
)

# Mapa de palavras-chave de alto sinal por skill (complementa o frontmatter/seção 2)
DOMAIN_BOOSTS: dict[str, tuple[str, ...]] = {
    "henke-audit-vibecode": ("audita o codigo", "revisa a qualidade", "vibecode", "gambiarra", "sr-only", "migue", "auditoria forense"),
    "henke-audit-seguranca": ("audita a seguranca", "vulnerabilidade", "vetor de ataque", "cve", "seguranca audit", "postura de seguranca"),
    "henke-audit-pentest": ("pentest", "red team", "teste de invasao", "idor", "bola", "race condition", "toctou", "mass assignment", "prototype pollution", "jwt none", "bypass de auth", "poc reproduzivel"),
    "henke-audit-cobertura": ("ledger de cobertura", "coverage_ledger", "auditoria completa com ledger", "90% de cobertura", "dossie de cobertura", "o que nao foi verificado"),
    "henke-arch-seguranca": ("deixa isso seguro", "chave exposta", "segredo vazado", "proteger antes de subir"),
    "henke-arch-cloud": ("arquitetura cloud", "multi-cloud", "google cloud", "na aws", "da aws", "no gcp", "cloudflare", "bucket s3", "iam policy", "least privilege", "menor privilegio", "oidc", "secrets manager", "terraform", "imdsv2", "anti-custo", "blinda a infra"),
    "henke-blindagem": ("3 gavetas", "idempotencia", "escrita atomica", "semaforo de gpu", "processo zumbi", "blindagem"),
    "henke-code-cirurgico": ("mudanca cirurgica", "bisturi", "alteracao pontual", "muda so essa linha", "sem quebrar nada"),
    "henke-code-ponte": ("contrato front e back", "tipos compartilhados", "ipc", "dados sumiram na tela", "campos vieram errados"),
    "henke-code-contratos": ("zod", "typebox", "pydantic", "openapi", "trpc", "json schema", "contrato formal", "schema compartilhado", "contrato de api", "fechamento de contrato", "contract_closure"),
    "henke-arch-banco": ("banco de dados", "modela o banco", "tabela", "database", "table", "schema", "migration", "migracao", "sqlite", "postgres", "sql"),
    "henke-debug-raiz": ("nao sei onde ta o erro", "causa raiz", "bissecta", "ta falhando mas nao sei", "investiga o bug"),
    "henke-plan-mvp": ("define o mvp", "primeira versao", "corta o escopo", "guilhotina"),
    "henke-plan-historias": ("quebra em tarefas", "escreve as historias", "cartoes de tarefa", "spec egm"),
    "henke-arch-pastas": ("organiza as pastas", "estrutura de pastas", "arquivos jogados na raiz"),
    "henke-arch-desmembrar": ("arquivo gigante", "arquivo de 2000 linhas", "desmembra"),
    "henke-ops-preflight": ("preflight", "pronto pra producao", "checklist antes de subir"),
    "henke-prepara": ("prepara o projeto", "inicia o henke", "henke-prepara", "organiza a memoria"),
}

STOPWORDS = {
    "para", "como", "quando", "onde", "qual", "quais", "isso", "esse", "essa",
    "este", "esta", "pelo", "pela", "com", "sem", "mais", "menos", "muito",
    "tudo", "nada", "fazer", "feito", "usuario", "pedir", "use", "usar",
    "projeto", "codigo", "arquivo", "arquivos", "antes", "depois", "uma", "uns",
}


def _parse_skill_file(skill_md: Path) -> dict:
    raw = skill_md.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    slug = skill_md.parent.name
    desc = ""
    when_not: list[str] = []
    fm = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.S)
    body = raw
    if fm:
        fm_text, body = fm.group(1), fm.group(2)
        m_desc = re.search(r"(?m)^description:\s*>?-?\s*\n((?:[ \t]+.+\n?)+)", fm_text)
        if m_desc:
            desc = " ".join(l.strip() for l in m_desc.group(1).splitlines())
        else:
            m_single = re.search(r"(?m)^description:\s*(.+)$", fm_text)
            if m_single:
                desc = m_single.group(1).strip().strip("\"'")
        m_wn_list = re.search(r"(?m)^when_not:\s*\n((?:[ \t]*-\s*.+\n?)+)", fm_text)
        if m_wn_list:
            for item_ln in m_wn_list.group(1).splitlines():
                val = re.sub(r"^[ \t]*-\s*", "", item_ln).strip().strip("\"'")
                if val:
                    when_not.append(val)
        else:
            m_wn = re.search(r"(?m)^when_not:\s*(.+)$", fm_text)
            if m_wn:
                when_not.extend(x.strip().strip("\"'") for x in m_wn.group(1).split(",") if x.strip())

    # Extrai frases-gatilho da seção "## 2. Ponto de Partida"
    triggers: list[str] = []
    sec2 = re.search(r"(?m)^## 2\..*?\n(.*?)(?=\n## |\Z)", body, flags=re.S)
    if sec2:
        for line in sec2.group(1).splitlines():
            for q in re.findall(r'"([^"]{4,})"', line):
                triggers.append(_norm(q))
            for c in re.findall(r"`/([a-z0-9-]+)`", line):
                triggers.append(_norm(c))

    # Extrai frases "Use quando..." da description
    norm_desc = _norm(desc)
    for m_use in re.findall(r'"([^"]{4,})"', norm_desc):
        triggers.append(m_use)

    for b in DOMAIN_BOOSTS.get(slug, ()):
        triggers.append(_norm(b))

    # Tokens distintivos do slug (ex: henke-audit-seguranca -> audit, seguranca)
    slug_parts = [p for p in slug.split("-")[1:] if len(p) >= 3 and p not in ("skills", "code", "plan", "arch", "mem", "ops")]

    return {
        "name": slug,
        "path": str(skill_md.resolve()).replace("\\", "/"),
        "description": desc,
        "triggers": sorted(set(t for t in triggers if t)),
        "slug_tokens": slug_parts,
        "when_not": [_norm(w) for w in when_not],
    }


def load_catalog(suite_root: Path) -> tuple[list[dict], list[str]]:
    skills = []
    unrouted = []
    for p in sorted(suite_root.glob("henke-*/SKILL.md")):
        if not p.is_file():
            continue
        info = _parse_skill_file(p)
        if not info["triggers"] and not info["slug_tokens"]:
            unrouted.append(info["name"])
        skills.append(info)
    return skills, unrouted


def _touched_files(repo_root: Path | None) -> list[str]:
    if not repo_root or not (repo_root / ".git").exists():
        return []
    try:
        res = subprocess.run(
            ["git", "-c", "core.quotePath=false", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        if res.returncode != 0:
            return []
        files = []
        for line in res.stdout.splitlines():
            if len(line) > 3:
                files.append(line[3:].strip().strip('"').replace("\\", "/"))
        return files
    except Exception:
        return []


def route_request(prompt: str, repo_root: Path | None = None, suite_root: Path | None = None) -> dict:
    if suite_root is None:
        suite_root = Path(__file__).resolve().parents[1]
    catalog, unrouted = load_catalog(suite_root)
    norm_p = _norm(prompt.strip())
    words = set(re.findall(r"[a-z0-9_]{3,}", norm_p)) - STOPWORDS

    excluded: set[str] = set()
    forced_mode: str | None = None
    matched_negatives: list[str] = []
    for rule in NEGATIVE_DISAMBIGUATION:
        if re.search(rule["when_matches"], norm_p):
            excluded.update(rule.get("exclude_skills", ()))
            matched_negatives.append(rule["id"])
            if rule.get("force_mode"):
                forced_mode = rule["force_mode"]

    requires_human_auth = any(sig in norm_p for sig in PUBLISH_SIGNALS) or any(sig in norm_p for sig in DESTRUCTIVE_SIGNALS)

    # Determinação de Modo com Veto de Segurança (§1)
    is_question = norm_p.startswith(INTERROGATIVE_PREFIXES) or norm_p.endswith("?")
    is_destructive_cmd = any(sig in norm_p for sig in DESTRUCTIVE_SIGNALS)

    if forced_mode:
        mode = forced_mode
    elif is_question:
        mode = "ANALISAR"
    elif is_destructive_cmd:
        mode = "EXECUTAR"
    elif any(s in norm_p for s in AUDIT_SIGNALS):
        mode = "ANALISAR"
    elif any(s in norm_p for s in PLAN_SIGNALS) and not any(s in norm_p for s in EXEC_SIGNALS):
        mode = "PLANEJAR"
    elif any(s in norm_p for s in EXEC_SIGNALS):
        mode = "EXECUTAR"
    else:
        mode = "ANALISAR"

    # Pontuação de cada skill contra o prompt
    scored = []
    for sk in catalog:
        name = sk["name"]
        if name == "henke-skills" or name in excluded:
            continue
        if any(wn and wn in norm_p for wn in sk["when_not"]):
            continue

        score = 0.0
        reasons = []
        if name in norm_p:
            score += 8.0
            reasons.append(f"menção explícita a '{name}'")

        for trig in sk["triggers"]:
            if len(trig) >= 5 and trig in norm_p:
                score += 4.5
                reasons.append(f"gatilho '{trig}'")
                break

        for tok in sk["slug_tokens"]:
            pref = tok[:6] if len(tok) >= 6 else tok
            if tok in words or (len(tok) >= 5 and any(w.startswith(pref) for w in words)):
                score += 2.0
                reasons.append(f"domínio '{tok}'")

        # Match moderado por termos distintivos da descrição
        desc_words = set(re.findall(r"[a-z0-9_]{4,}", _norm(sk["description"]))) - STOPWORDS
        overlap = words & desc_words
        if len(overlap) >= 2:
            score += min(2.5, 0.7 * len(overlap))
            reasons.append(f"termos ({', '.join(sorted(overlap)[:3])})")

        # Alinhamento de família com o modo
        if mode == "ANALISAR" and name.startswith(("henke-audit-", "henke-debug-")):
            score *= 1.15
        elif mode == "PLANEJAR" and name.startswith(("henke-plan-", "henke-arch-")):
            score *= 1.15
        elif mode == "EXECUTAR" and name.startswith(("henke-code-", "henke-ui-", "henke-refactor-")):
            score *= 1.15

        if score >= 1.8:
            scored.append((round(score, 2), name, sk["path"], "; ".join(reasons[:2])))

    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:3]

    if not top:
        confidence = "none"
    elif top[0][0] >= 4.0:
        confidence = "high"
    elif top[0][0] >= 2.5:
        confidence = "medium"
    else:
        confidence = "low"

    # Se confiança for baixa e for pergunta trivial fora de engenharia, zera skills
    if confidence == "low" and not any(s in norm_p for s in AUDIT_SIGNALS + PLAN_SIGNALS + EXEC_SIGNALS):
        top = []
        confidence = "none"

    # Seleção do Arquétipo e Portões Mecânicos Obrigatórios
    lead_skill = top[0][1] if top else ""
    if mode == "ANALISAR" or lead_skill.startswith(("henke-audit-", "henke-debug-")):
        archetype = "SHERLOCK"
        gates_required = [
            "scripts/repo_signals.py",
            "scripts/metricas/hazard_scan.py",
            "scripts/metricas/assertless_scan.py",
            "scripts/metricas/coverage_ledger.py",
        ]
    elif mode == "PLANEJAR" or lead_skill.startswith(("henke-plan-", "henke-arch-")):
        archetype = "ESTADISTA"
        gates_required = [
            "scripts/spec_drift.py",
            "scripts/metricas/contract_closure.py",
            "scripts/metricas/fence_check.py",
        ]
    else:
        archetype = "CARPINTEIRO"
        gates_required = [
            "scripts/spec_drift.py",
            "scripts/metricas/ratchet.py",
            "scripts/metricas/hazard_scan.py",
            "scripts/metricas/assertless_scan.py",
            "scripts/metricas/fence_check.py",
            "scripts/metricas/contract_closure.py",
            "scripts/metricas/metaconfig_diff.py",
        ]

    scope_paths = _touched_files(repo_root)

    return {
        "mode": mode,
        "archetype": archetype,
        "confidence": confidence,
        "requires_human_auth": requires_human_auth,
        "skills": [
            {"name": name, "path": path, "score": sc, "reason": rsn}
            for sc, name, path, rsn in top
        ],
        "gates_required": gates_required if confidence != "none" else [
            "scripts/repo_signals.py",
            "scripts/metricas/hazard_scan.py",
        ],
        "scope_paths": scope_paths,
        "negative_rules_triggered": matched_negatives,
        "unrouted_catalog_skills": unrouted,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Roteador determinístico de skills da Suíte Henke.")
    ap.add_argument("--prompt", required=True, help="Pedido do usuário em linguagem natural")
    ap.add_argument("--repo", default=None, help="Diretório do repositório alvo (opcional)")
    ap.add_argument("--suite-root", default=None, help="Raiz da Suíte Henke (opcional)")
    args = ap.parse_args()

    if not args.prompt or not args.prompt.strip():
        print("ERRO: --prompt não pode ser vazio.", file=sys.stderr)
        sys.exit(2)

    repo_root = Path(args.repo).resolve() if args.repo else None
    if repo_root is not None and not repo_root.exists():
        print(f"ERRO: diretório --repo inexistente: {repo_root}", file=sys.stderr)
        sys.exit(2)

    suite_root = Path(args.suite_root).resolve() if args.suite_root else Path(__file__).resolve().parents[1]
    if not suite_root.is_dir():
        print(f"ERRO: diretório --suite-root inválido: {suite_root}", file=sys.stderr)
        sys.exit(2)

    result = route_request(args.prompt, repo_root=repo_root, suite_root=suite_root)
    try:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.flush()
    except BrokenPipeError:
        sys.exit(2)


if __name__ == "__main__":
    main()
