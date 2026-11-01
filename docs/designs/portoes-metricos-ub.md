# Design Spec — Portões Métricos (assimilação Uncle Bob Skills) — v2 correção adversarial

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py`. Alterá-la após aprovação invalida a aprovação.

> **v2 (2026-10-31):** a perícia adversarial independente sobre `d05880c` reproduziu 5 falhas contra `virusclip` (repo real TS/Vitest): falso-verde do `fence_check` com `edges=[]`, crash do `churn` em commits com binários, miopia Python-only, crash cp1252/`rglob` atravessando `node_modules`, denominador errado do benchmark e skills sem instrução dos portões. Esta revisão universaliza coletores/scanners (py/ts/tsx/js/mjs/rs/go), corrige o falso-verde, endurece streams Windows, instrui as skills e exige prova física no `virusclip`.

## 1. Contexto

A auditoria comparativa Suíte Henke × `Island-Dev-Crew/uncle-bob-skills` (conversa de 2026-10-31) identificou que ~60% do pacote UB já é coberto pelo PROTOCOLO (custódia, spec-hash, ledger, canários, dois agentes). A lacuna real é **métrica de qualidade executável**: hoje `code-cirurgico`, `arch-pastas` e a regra anti-migué de cobertura operam só em `[PROSA]`. Esta spec porta 5 portões determinísticos do UB + coletores locais, transformando-os em `[MECÂNICO]`.

Decisão arquitetural: **zero skills novas**. Os portões viram mecanismos em `scripts/metricas/`, invocáveis por `henke-audit-vibecode`, `henke-ops-preflight` e pelo Maestro — sem inflar o inventário de 49 skills.

## 2. Decisões

- Portar (MIT, com atribuição no cabeçalho) e não reescrever: menos código novo, semântica já endurecida.
- Somente portões em Python stdlib → roda no CI Windows/Linux existente sem dependências novas.
- Scripts `.sh` do UB **não** são portados (mutant-hunt fica `[A CONSTRUIR]` — exige mutmut/Stryker por linguagem).
- Coletores locais separam "medir" de "julgar": os portões UB só julgam TSV/JSON.
- Disciplina de exit herdada: `0`=verde, `1`=veredito-vermelho, `2`=não-veredito (fail closed, inclui stdout fechado).

## 3. Contratos

| Portão | Entrada | Vermelho quando |
|---|---|---|
| `assertless_scan.py` | arquivos `test_*.py` (AST, nunca importa) | teste sem asserção, mock-only ou except engolidor |
| `crap_score.py` | TSV `func<TAB>complexidade<TAB>cov%` | CRAP(m) > threshold |
| `ratchet.py` | TSV baseline vs. atual `path<TAB>worst_crap<TAB>max_comp<TAB>cov%` | arquivo tocado saiu pior / novo arquivo acima do teto |
| `fence_check.py` | `fence.json` (camadas) + `edges.json` (imports) | aresta apontando "para fora" da camada |
| `tornado_scan.py` | TSV `mudança<TAB>arquivos_tocados` | tendência de amplificação de mudança subindo |

Coletor `scripts/repo_signals.py` (stdlib): `churn` (git numstat → TSV), `metrics` (AST → complexidade por função + coverage opcional), `edges` (AST imports → edges.json).

## 4. Escopo Declarado (arquivos e símbolos a tocar)

Novos (v1):
- `docs/designs/portoes-metricos-ub.md` (este arquivo)
- `scripts/metricas/__init__.py`
- `scripts/metricas/assertless_scan.py`
- `scripts/metricas/crap_score.py`
- `scripts/metricas/ratchet.py`
- `scripts/metricas/fence_check.py`
- `scripts/metricas/tornado_scan.py`
- `scripts/repo_signals.py`
- `evals/canaries_metricas.py`
- `tests/test_metric_gates.py`
- `evals/bench_corpus.py`

Novos (v2):
- `scripts/metricas/polyglot.py` — lexer O(N) compartilhado (mask de comentários/strings, funções por chaveamento, blocos de teste, imports) para ts/tsx/js/mjs/rs/go.

Editados (v1):
- `PROTOCOLO.md` — §10 tabela de portões + delta de versão (v3.1.1 → v3.2)
- `README.md` — seção de verificação (1 item)
- `.github/workflows/ci.yml` — +2 passos (test_metric_gates, canaries_metricas)

Editados (v2):
- `docs/designs/portoes-metricos-ub.md` — esta revisão
- `scripts/metricas/fence_check.py` — `edges=[]` → exit 2 (mata falso-verde)
- `scripts/repo_signals.py` — universalização: `os.walk` com poda (`node_modules`, `target`, `dist`, `out`, `release`, `vendor`, `.runtime`, `coverage`), churn conta binários/`--no-merges`/nunca emite 0, metrics poliglota, loaders de coverage (coverage.py, Istanbul summary/final, lcov.info), edges poliglota (py/ts/js/rs/go + pacotes de workspace)
- `scripts/metricas/assertless_scan.py` — dispatch poliglota (`.test.ts`, `.spec.ts`, `.test.js`, `_test.go`, `_test.rs`, `tests/*.rs`)
- `scripts/metricas/*.py` + `scripts/repo_signals.py` — reconfiguração de streams para UTF-8 em Windows
- `henke-audit-vibecode/SKILL.md`, `henke-code-cirurgico/SKILL.md`, `henke-arch-pastas/SKILL.md`, `henke-ops-preflight/SKILL.md`, `henke-skills/SKILL.md` — instrução dos portões + heurística universal por linguagem
- `evals/bench_corpus.py` — denominador correto (defeitos ≠ controles) + defeitos TS (D10 teste-fantasma `.test.ts`, D11 função complexa `.ts`, D12 dependência outward `.ts`)
- `evals/canaries_metricas.py` — canários: fence `[]`→2, assertless `.test.ts` bilateral, edges `./helper.js`→`.ts`, churn commit só-binário
- `tests/test_metric_gates.py` — cobertura dos novos caminhos

Fora de escopo (declarado): `install*`, `prompts/`, `.henke/`, nenhum arquivo do `virusclip` (verificação física é read-only).

## 5. Casos de borda

- CRLF/Windows: portões leem TSV com `\r\n`; collectors normalizam.
- stdout fechado (`| head -1`): exit 2, nunca 1 falso-verde.
- Escopo vazio (diff só-docs, repo sem testes): veredito vazio registrado como evidência, não erro.
- coverage ausente: `metrics` documenta `cov=0` como pior caso declarado, não fabrica número.
- Caminho permitido obrigatório: todo canário testa reprovação **e** aprovação.

## 6. Ledger esperado

- `python tests/test_metric_gates.py` → verde
- `python evals/canaries_metricas.py` → N/N canários
- `python evals/bench_corpus.py` → tabela de recall impressa
- `python scripts/validate_skills.py` → verde (regressão)
- `python tests/test_evals.py` + `python evals/canaries.py` → verde (regressão)

## 7. O que NÃO será verificado

- mutant-hunt (sem runtime de mutação nesta spec) → permanece `[A CONSTRUIR]`.
- Cobertura por função em TS: loaders aceitos agregam por arquivo; per-function em JS exige instrumentação fora do stdlib — declarado.
- Dispatch automático de coverage: o coletor aceita `--coverage-json`/`--lcov`, mas quem produz o relatório é a toolchain do projeto (vitest/coverage.py/go test) — `[PROSA]` no SKILL.
- Scanners léxicos (não-AST) são aproximações O(N): generics TS com `<`/`>` aninhados, macros Rust complexos e regex literals ambíguos podem deslocar fronteiras de função — erro admissível declarado, sem crash e sem falso-verde.
- closed-stream/lane-check aplicados aos portões **antigos** (hardening retroativo — P2 futuro).
