#!/usr/bin/env python3
"""
henke_router.py — Roteador determinístico de catálogo da Suíte Henke (PROTOCOLO v4.0).

Inspirado na disciplina do `nvidia-skill-finder` (NVIDIA/skills):
- Lê os `SKILL.md` como fonte única da verdade (`description`, `## 2. Ponto de Partida`
  e `when_not` opcional no frontmatter).
- Opera como veto de Modo (`ANALISAR` default sob ambiguidade/pergunta; `requires_human_auth`
  quando detecta vocabulário destrutivo ou `PUBLISH`).
- Usa tabela declarativa de desambiguação negativa (`NEGATIVE_DISAMBIGUATION`).
- Permite `skills: []` (`confidence: "none"`, exit 0) quando o pedido não pertence ao
  catálogo Henke — nunca força pipeline fixo (§8).
- Matching de gatilhos com flexão morfológica PT simples (`_has_flex_signal`):
  gênero (o/a), número (s/es), artigos (um/uma/o/a) e -ão/-m/-l/-r.
- Fallback de engenharia: prompt técnico sem match recebe a skill coringa do modo
  (`FALLBACK_SKILL_BY_MODE`) sem inflar `confidence` — fica none/low, honesto.

Códigos de saída:
  0  roteamento concluído (inclui 0 skills quando fora de escopo)
  2  uso incorreto, prompt vazio, diretório inexistente ou stdout fechado
"""
from __future__ import annotations

import argparse
from functools import lru_cache
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


def _has_signal(text: str, sig: str) -> bool:
    """Verifica sinal com word-boundary para palavras simples, evitando colisões (check vs checklist)."""
    sig_clean = sig.strip()
    if not sig_clean:
        return False
    if re.fullmatch(r"[\w-]+", sig_clean):
        return bool(re.search(r"\b" + re.escape(sig_clean) + r"\b", text))
    prefix = r"\b" if sig_clean[0].isalnum() else ""
    suffix = r"\b" if sig_clean[-1].isalnum() else ""
    return bool(re.search(prefix + re.escape(sig_clean) + suffix, text))


# Flexão morfológica PT para matching de gatilhos (sem libs externas).
# Artigos viram uma única classe (cobre um/uma/o/a) e substantivos/adjetivos
# ganham variantes de gênero e número por sufixo.
_FLEX_ARTICLES = frozenset({
    "o", "a", "os", "as", "um", "uma", "uns", "umas",
    "do", "da", "dos", "das", "no", "na", "nos", "nas", "ao", "aos",
})
_FLEX_ARTICLE_RE = r"(?:os?|as?|um|uma|uns|umas|d[oa]s?|n[oa]s?|a[oa]s?)"

# Finais que indicam adjetivo em PT — únicos onde o<->a é seguro flexionar.
_FLEX_GENDER_SUF = (
    "ivo", "iva", "oso", "osa", "ado", "ada", "ido", "ida", "ito", "ita",
    "eiro", "eira", "orio", "oria", "ario", "aria", "ento", "enta",
    "ovo", "ova", "ilo", "ila",
)


def _flex_word(tok: str) -> str:
    if len(tok) < 3:
        return re.escape(tok)
    if tok in _FLEX_ARTICLES:
        return _FLEX_ARTICLE_RE
    if tok.endswith(("oes", "aes")):
        return re.escape(tok[:-3]) + r"(?:ao|oes|aes)"
    if tok.endswith("ao"):
        return re.escape(tok[:-2]) + r"(?:ao|oes|aes)"
    if len(tok) >= 5 and tok.endswith("eis"):
        return re.escape(tok[:-3]) + r"(?:el|eis)"
    if len(tok) >= 5 and tok.endswith("ais"):
        return re.escape(tok[:-3]) + r"(?:al|ais)"
    # Troca de gênero o<->a só em final adjetival ('responsivo'->responsiv[oa]);
    # nunca em substantivo ('bola' não pode casar 'bolo' — canário M45).
    for suf in _FLEX_GENDER_SUF:
        if tok.endswith(suf):
            return re.escape(tok[: -len(suf)] + suf[:-1]) + r"[oa]s?"
        if len(tok) > len(suf) + 1 and tok.endswith(suf + "s"):
            return re.escape(tok[: -len(suf) - 1] + suf[:-1]) + r"[oa]s?"
    if tok.endswith("es"):
        return re.escape(tok[:-2]) + r"es?"
    if tok.endswith("m"):
        return re.escape(tok[:-1]) + r"(?:m|ns)"
    if tok.endswith("l"):
        return re.escape(tok[:-1]) + r"(?:l|is)"
    if tok.endswith(("r", "z")):
        return re.escape(tok) + r"es?"
    if tok.endswith("s"):
        return re.escape(tok[:-1]) + r"s?"
    if tok.endswith("x"):
        return re.escape(tok)
    return re.escape(tok) + r"s?"


@lru_cache(maxsize=1024)
def _flex_pattern(sig: str) -> str:
    parts = []
    for chunk in re.findall(r"[a-z0-9_]+|[^a-z0-9_]+", sig):
        if re.fullmatch(r"[a-z0-9_]+", chunk):
            parts.append(_flex_word(chunk))
        elif chunk.isspace():
            parts.append(r"\s+")
        else:
            parts.append(re.escape(chunk))
    pat = "".join(parts)
    if not pat:
        return ""
    prefix = r"\b" if sig[0].isalnum() else ""
    suffix = r"\b" if sig[-1].isalnum() else ""
    return prefix + pat + suffix


def _has_flex_signal(text: str, sig: str) -> bool:
    """_has_signal com variantes flexivas PT — usado só no matching de gatilhos."""
    pat = _flex_pattern(sig.strip())
    return bool(pat) and bool(re.search(pat, text))


# Vocabulário destrutivo e PUBLISH (PROTOCOLO §1) — exige autorização humana explícita
DESTRUCTIVE_SIGNALS = (
    "deleta", "deletar", "deleto", "deletamos", "delete", "exclui", "excluir",
    "excluo", "excluímos", "apago", "apaga", "apagar", "apague", "apagamos",
    "remove", "remover", "removo", "remova", "removendo", "erase", "erases",
    "drop", "drop table", "truncate", "wipe", "wipe database",
    "destroi", "destruo", "destruir", "destruímos", "destroy", "purge",
    "force-push", "push --force", "force push", "push -f", "reset --hard",
    "hard reset", "rm -rf", "rm -r", "git clean", "clean -fd", "clean -fdx",
    "apaga o banco", "apagar o banco", "derruba", "zera", "zerar",
)
_DESTRUCTIVE_SIGNALS = DESTRUCTIVE_SIGNALS

PUBLISH_ONLY_SIGNALS = (
    "deploy", "publica", "publicar", "producao", "production",
    "sobe", "subir", "pro ar", "no ar", "em producao",
    "publish", "npm publish", "pypi", "release", "lanca release", "git push",
    "push na main", "push pra main", "push origin",
    "faxina", "limpa o repo", "apaga arquivos", "reversao", "reverte", "rollback",
)
PUBLISH_SIGNALS = PUBLISH_ONLY_SIGNALS + DESTRUCTIVE_SIGNALS
_PUBLISH_SIGNALS = PUBLISH_SIGNALS

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
_AUDIT_SIGNALS = AUDIT_SIGNALS

PLAN_SIGNALS = (
    "planeja", "plano", "spec", "mvp", "historias", "cartoes de tarefa",
    "fluxo de telas", "jornada do usuario", "ideia de app", "referencias de mercado",
    "pre-mortem", "arquitetura de pastas", "qual stack", "modela o banco",
    "plan", "roadmap", "user stories", "task cards", "user journey", "screen flow",
    "app idea", "market references", "folder architecture", "database model",
)
_PLAN_SIGNALS = PLAN_SIGNALS

EXEC_SIGNALS = (
    "implementa", "cria", "construa", "corrige", "conserta", "edita",
    "adiciona", "refatora", "desmembra", "renomeia", "integra", "protege",
    "prepara o projeto", "inicia o henke", "mudanca", "muda", "alinha", "ajusta",
    "atualiza", "aplica", "faz", "faze", "fazer", "mete", "bota", "botar",
    "arruma", "arrumar",
    "deploy", "publica", "publicar", "sobe", "subir", "pro ar", "no ar",
    "em producao", "deleta", "deletar", "delete", "exclui", "excluir",
    "drop", "wipe", "truncate", "destroy", "purge",
    "implement", "create", "build", "fix", "patch", "edit", "add", "refactor",
    "split", "rename", "integrate", "protect", "update", "align", "adjust", "apply",
)
_EXEC_SIGNALS = EXEC_SIGNALS

COMPOUND_FIX_VERBS = (
    "corrige", "corrigir", "conserta", "consertar", "implementa", "implementar",
    "fix", "patch", "refatora", "refatorar", "aplica", "aplicar",
)

UI_SIGNALS = (
    "tela", "interface", "layout", "botao", "modal", "css", "tailwind",
    "cores", "paleta", "tipografia", "fonte", "responsivo", "mobile",
    "loading", "skeleton", "hover", "acessibilidade", "a11y", "contraste",
)
_UI_SIGNALS = UI_SIGNALS

GENERIC_SINGLE_TRIGGERS = {
    "audita", "auditar", "cria", "criar", "limpa", "limpar", "revisa", "revisar",
    "testa", "testar", "arruma", "arrumar", "corrige", "corrigir", "ajusta", "ajustar",
    "muda", "mudar", "adiciona", "adicionar", "verifica", "verificar", "checa", "checar",
    "melhora", "melhorar", "organiza", "organizar", "explica", "explicar",
    "faz", "faze", "fazer", "mete", "bota", "botar", "sobe", "subir",
    "deploy", "publica", "publicar",
}

# Termos de engenharia que ligam o fallback honesto por modo.
_ENGINEERING_TERMS = re.compile(
    r"\b(?:bugs?|erros?|telas?|logins?|apis?|bancos?|testes?|builds?|quebr\w*|falh\w*)\b"
)

# Skill coringa por modo quando o pedido é de engenharia mas nada pontuou.
FALLBACK_SKILL_BY_MODE = {
    "ANALISAR": "henke-debug-raiz",
    "EXECUTAR": "henke-code-cirurgico",
    "PLANEJAR": "henke-plan-destrava",
}

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
    {
        "id": "mutation_testing_is_not_coverage_ledger",
        "when_matches": r"\b(teste de mutacao|testes de mutacao|mutant_hunt|mutantes? sobreviventes?|kill rate|mata mutantes)\b",
        "exclude_skills": ("henke-audit-cobertura", "henke-debug-sanidade"),
    },
    {
        "id": "banca_multi_model_is_not_single_vibecode",
        "when_matches": r"\b(banca adversarial|multi-modelo|consenso da banca|todos contra todos|quorum 2/3|juri de modelos)\b",
        "exclude_skills": ("henke-audit-vibecode",),
    },
    {
        "id": "perf_audit_is_not_sec_audit",
        "when_matches": r"\b(queries n\+1|n\+1 query|io sincrono em async|bloqueio de event loop|latencia p99|audita a performance|gargalo de cpu)\b",
        "exclude_skills": ("henke-audit-seguranca", "henke-ui-auditoria"),
    },
)

# Mapa de palavras-chave de alto sinal por skill (complementa o frontmatter/seção 2)
DOMAIN_BOOSTS: dict[str, tuple[str, ...]] = {
    "henke-audit-vibecode": ("audita o codigo", "revisa a qualidade", "vibecode", "gambiarra", "sr-only", "migue", "auditoria forense"),
    "henke-audit-seguranca": ("audita a seguranca", "vulnerabilidade", "vetor de ataque", "cve", "seguranca audit", "postura de seguranca"),
    "henke-audit-pentest": ("pentest", "red team", "teste de invasao", "idor", "bola", "race condition", "toctou", "mass assignment", "prototype pollution", "jwt none", "bypass de auth", "poc reproduzivel"),
    "henke-audit-cobertura": ("ledger de cobertura", "coverage_ledger", "auditoria completa com ledger", "90% de cobertura", "dossie de cobertura", "o que nao foi verificado"),
    "henke-audit-banca": ("banca adversarial", "multi-modelo", "consenso da banca", "todos contra todos", "quorum 2/3", "goldfish multi-modelo", "auditoria em banca", "juri de modelos"),
    "henke-test-mutantes": ("teste de mutacao", "testes de mutacao", "mutant_hunt", "mutantes sobreviventes", "kill rate", "mata mutantes", "mutacao de ast", "testes fracos"),
    "henke-audit-perf": ("audita a performance", "diagnostico de performance", "queries n+1", "n+1 query", "hotspot crap", "gargalo de cpu", "bloqueio de event loop", "io sincrono em async", "latencia p99", "benchmark antes e depois"),
    "henke-code-llm": ("integracao llm", "integra llm", "prompt injection", "structured output", "rag seguro", "agente de ia", "chamada de llm", "fallback de modelo", "orcamento de tokens", "tool calling"),
    "henke-ops-observa": ("observabilidade", "logs estruturados", "correlacao de trace_id", "run_id nos logs", "telemetria", "metricas de producao", "catch cego", "rastreamento de erros", "opentelemetry"),
    "henke-arch-seguranca": ("deixa isso seguro", "chave exposta", "segredo vazado", "proteger antes de subir"),
    "henke-arch-cloud": ("arquitetura cloud", "multi-cloud", "google cloud", "na aws", "da aws", "no gcp", "cloudflare", "bucket s3", "iam policy", "least privilege", "menor privilegio", "oidc", "secrets manager", "terraform", "imdsv2", "anti-custo", "blinda a infra"),
    "henke-blindagem": ("3 gavetas", "idempotencia", "escrita atomica", "semaforo de gpu", "processo zumbi", "blindagem"),
    "henke-code-cirurgico": ("mudanca cirurgico", "mudanca cirurgica", "bisturi", "alteracao pontual", "muda so essa linha", "sem quebrar nada"),
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

    domain_boosts = [_norm(b) for b in DOMAIN_BOOSTS.get(slug, ())]
    triggers.extend(domain_boosts)

    # Tokens distintivos do slug (ex: henke-audit-seguranca -> audit, seguranca)
    slug_parts = [p for p in slug.split("-")[1:] if len(p) >= 3 and p not in ("skills", "code", "plan", "arch", "mem", "ops", "test")]

    return {
        "name": slug,
        "path": str(skill_md.resolve()).replace("\\", "/"),
        "description": desc,
        "triggers": sorted(set(t for t in triggers if t)),
        "domain_boosts": set(domain_boosts),
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

    is_question = norm_p.startswith(INTERROGATIVE_PREFIXES) or norm_p.endswith("?")
    requires_human_auth = (
        any(_has_signal(norm_p, sig) for sig in PUBLISH_SIGNALS)
        or any(_has_signal(norm_p, sig) for sig in DESTRUCTIVE_SIGNALS)
    )

    # Determinação de Modo com Veto de Segurança (§1)
    is_destructive_cmd = any(_has_signal(norm_p, sig) for sig in DESTRUCTIVE_SIGNALS)
    has_audit = any(_has_signal(norm_p, s) for s in AUDIT_SIGNALS)
    has_plan = any(_has_signal(norm_p, s) for s in PLAN_SIGNALS)
    has_exec = any(_has_signal(norm_p, s) for s in EXEC_SIGNALS)
    has_compound_fix = any(_has_signal(norm_p, v) for v in COMPOUND_FIX_VERBS)
    is_compound_exec = (not is_question) and has_audit and has_compound_fix

    if forced_mode:
        mode = forced_mode
    elif is_question:
        mode = "ANALISAR"
    elif is_destructive_cmd or is_compound_exec:
        mode = "EXECUTAR"
    elif has_audit:
        mode = "ANALISAR"
    elif has_plan and not has_exec:
        mode = "PLANEJAR"
    elif has_exec:
        mode = "EXECUTAR"
    else:
        mode = "ANALISAR"

    scope_paths = _touched_files(repo_root)
    has_ui_touched = any(p.lower().endswith((".tsx", ".jsx", ".css", ".vue", ".svelte")) for p in scope_paths)

    # Pontuação de cada skill contra o prompt
    scored = []
    for sk in catalog:
        name = sk["name"]
        if name == "henke-skills" or name in excluded:
            continue
        if any(wn and _has_signal(norm_p, wn) for wn in sk["when_not"]):
            continue

        score = 0.0
        reasons = []
        if name in norm_p:
            score += 8.0
            reasons.append(f"menção explícita a '{name}'")

        desc_words = set(re.findall(r"[a-z0-9_]{4,}", _norm(sk["description"]))) - STOPWORDS
        context_tokens = set(sk["slug_tokens"]) | desc_words
        domain_boosts = sk.get("domain_boosts", set())

        # Avalia primeiro gatilhos multi-palavra e domain_boosts; gatilhos curtos genéricos de 1 palavra exigem contexto
        sorted_trigs = sorted(sk["triggers"], key=lambda t: (0 if (" " in t.strip() or t in domain_boosts) else 1, -len(t)))
        for trig in sorted_trigs:
            if len(trig) >= 4 and _has_flex_signal(norm_p, trig):
                is_single_generic = (
                    trig in GENERIC_SINGLE_TRIGGERS
                    or (" " not in trig.strip() and trig not in domain_boosts and not trig.startswith("henke-"))
                )
                if is_single_generic:
                    has_ctx = any(
                        tok in words or (len(tok) >= 5 and any(w.startswith(tok[:6]) for w in words))
                        for tok in context_tokens if tok != trig
                    )
                    if has_ctx:
                        score += 4.5
                        reasons.append(f"gatilho '{trig}'")
                        break
                    else:
                        score += 0.5
                else:
                    score += 4.5
                    reasons.append(f"gatilho '{trig}'")
                    break

        for tok in sk["slug_tokens"]:
            pref = tok[:6] if len(tok) >= 6 else tok
            if tok in words or (len(tok) >= 5 and any(w.startswith(pref) for w in words)):
                score += 2.0
                reasons.append(f"domínio '{tok}'")

        # Match moderado por termos distintivos da descrição
        overlap = words & desc_words
        if len(overlap) >= 2:
            score += min(2.5, 0.7 * len(overlap))
            reasons.append(f"termos ({', '.join(sorted(overlap)[:3])})")

        # Afinidade por escopo de arquivos modificados no repositório (git status)
        if has_ui_touched and name.startswith("henke-ui-") and score > 0:
            score += 1.0
            reasons.append("afinidade git UI")

        # Alinhamento de família com o modo
        if mode == "ANALISAR" and name.startswith(("henke-audit-", "henke-debug-", "henke-test-")):
            score *= 1.15
        elif mode == "PLANEJAR" and name.startswith(("henke-plan-", "henke-arch-")):
            score *= 1.15
        elif mode == "EXECUTAR" and name.startswith(("henke-code-", "henke-ui-", "henke-refactor-", "henke-ops-", "henke-test-")):
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
    if confidence == "low" and not (has_audit or has_plan or has_exec):
        top = []
        confidence = "none"

    # Fallback de engenharia: prompt técnico sem match útil recebe a skill coringa
    # do modo, sem inflar confidence — fica none/low, honesto (§8).
    if confidence in ("none", "low") and _ENGINEERING_TERMS.search(norm_p):
        fb_name = FALLBACK_SKILL_BY_MODE.get(mode)
        if fb_name and fb_name not in excluded and all(fb_name != n for _, n, _, _ in top):
            fb_skill = next((sk for sk in catalog if sk["name"] == fb_name), None)
            if fb_skill is not None and not any(
                wn and _has_signal(norm_p, wn) for wn in fb_skill["when_not"]
            ):
                top = (top + [(1.9, fb_name, fb_skill["path"],
                               f"fallback de engenharia ({mode})")])[:3]

    # Seleção do Arquétipo e Portões Mecânicos Obrigatórios
    lead_skill = top[0][1] if top else ""
    if is_compound_exec:
        archetype = "CARPINTEIRO"
        gates_required = [
            "scripts/repo_signals.py",
            "scripts/spec_drift.py",
            "scripts/metricas/ratchet.py",
            "scripts/metricas/hazard_scan.py",
            "scripts/metricas/assertless_scan.py",
            "scripts/metricas/fence_check.py",
            "scripts/metricas/contract_closure.py",
            "scripts/metricas/metaconfig_diff.py",
        ]
    elif mode == "ANALISAR" or (mode != "EXECUTAR" and lead_skill.startswith(("henke-audit-", "henke-debug-"))):
        archetype = "SHERLOCK"
        gates_required = [
            "scripts/repo_signals.py",
            "scripts/metricas/hazard_scan.py",
            "scripts/metricas/assertless_scan.py",
            "scripts/metricas/coverage_ledger.py",
        ]
    elif mode == "PLANEJAR" or (mode != "EXECUTAR" and lead_skill.startswith(("henke-plan-", "henke-arch-"))):
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

    return {
        "mode": mode,
        "archetype": archetype,
        "confidence": confidence,
        "requires_human_auth": requires_human_auth,
        "skills": [
            {"name": name, "path": path, "score": sc, "reason": rsn}
            for sc, name, path, rsn in top
        ],
        "gates_required": gates_required if top else [
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
    ap.add_argument("--compact", action="store_true", help="Emite saída JSON enxuta para economia de tokens")
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
        if args.compact:
            compact_out = {
                "mode": result["mode"],
                "archetype": result["archetype"],
                "confidence": result["confidence"],
                "requires_human_auth": result["requires_human_auth"],
                "gates_required": result["gates_required"],
                "skills": [{"name": s["name"], "score": s["score"]} for s in result["skills"]],
            }
            print(json.dumps(compact_out, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.flush()
    except BrokenPipeError:
        sys.exit(2)


if __name__ == "__main__":
    main()
