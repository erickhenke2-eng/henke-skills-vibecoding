# Design Spec — Portões Métricos (assimilação Uncle Bob Skills) — v2 correção adversarial

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py`. Alterá-la após aprovação invalida a aprovação.

> **v2 (2026-10-31):** a perícia adversarial independente sobre `d05880c` reproduziu 5 falhas contra `virusclip` (repo real TS/Vitest): falso-verde do `fence_check` com `edges=[]`, crash do `churn` em commits com binários, miopia Python-only, crash cp1252/`rglob` atravessando `node_modules`, denominador errado do benchmark e skills sem instrução dos portões. Esta revisão universaliza coletores/scanners (py/ts/tsx/js/mjs/rs/go), corrige o falso-verde, endurece streams Windows, instrui as skills e exige prova física no `virusclip`.

## 1. Contexto

A auditoria comparativa Suíte Henke × `Island-Dev-Crew/uncle-bob-skills` (conversa de 2026-10-31) identificou que ~60% do pacote UB já é coberto pelo PROTOCOLO (custódia, spec-hash, ledger, canários, dois agentes). A lacuna real é **métrica de qualidade executável**: hoje `code-cirurgico`, `arch-pastas` e a regra anti-migué de cobertura operam só em `[PROSA]`. Esta spec porta 5 portões determinísticos do UB + coletores locais, transformando-os em `[MECÂNICO]`.

Decisão arquitetural: **zero skills novas**. Os portões viram mecanismos em `henke/metricas/`, invocáveis por `henke-audit-vibecode`, `henke-ops-preflight` e pelo Maestro — sem inflar o inventário de 49 skills.

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

Coletor `henke/repo_signals.py` (stdlib): `churn` (git numstat → TSV), `metrics` (AST → complexidade por função + coverage opcional), `edges` (AST imports → edges.json).

## 4. Escopo Declarado (arquivos e símbolos a tocar)

Novos (v1):
- `docs/designs/portoes-metricos-ub.md` (este arquivo)
- `henke/metricas/__init__.py`
- `henke/metricas/assertless_scan.py`
- `henke/metricas/crap_score.py`
- `henke/metricas/ratchet.py`
- `henke/metricas/fence_check.py`
- `henke/metricas/tornado_scan.py`
- `henke/repo_signals.py`
- `evals/canaries_metricas.py`
- `tests/test_metric_gates.py`
- `evals/bench_corpus.py`

Novos (v2):
- `henke/metricas/polyglot.py` — lexer O(N) compartilhado (mask de comentários/strings, funções por chaveamento, blocos de teste, imports) para ts/tsx/js/mjs/rs/go.

Editados (v1):
- `PROTOCOLO.md` — §10 tabela de portões + delta de versão (v3.1.1 → v3.2)
- `README.md` — seção de verificação (1 item)
- `.github/workflows/ci.yml` — +2 passos (test_metric_gates, canaries_metricas)

Editados (v2):
- `docs/designs/portoes-metricos-ub.md` — esta revisão
- `henke/metricas/fence_check.py` — `edges=[]` → exit 2 (mata falso-verde)
- `henke/repo_signals.py` — universalização: `os.walk` com poda (`node_modules`, `target`, `dist`, `out`, `release`, `vendor`, `.runtime`, `coverage`), churn conta binários/`--no-merges`/nunca emite 0, metrics poliglota, loaders de coverage (coverage.py, Istanbul summary/final, lcov.info), edges poliglota (py/ts/js/rs/go + pacotes de workspace)
- `henke/metricas/assertless_scan.py` — dispatch poliglota (`.test.ts`, `.spec.ts`, `.test.js`, `_test.go`, `_test.rs`, `tests/*.rs`)
- `henke/metricas/*.py` + `henke/repo_signals.py` — reconfiguração de streams para UTF-8 em Windows
- `henke-audit-vibecode/SKILL.md` — portões + heurística universal
- `henke-code-cirurgico/SKILL.md` — idem
- `henke-arch-pastas/SKILL.md` — idem
- `henke-ops-preflight/SKILL.md` — idem
- `henke-skills/SKILL.md` — idem
- `evals/bench_corpus.py` — denominador correto (defeitos ≠ controles) + defeitos TS (D10 teste-fantasma `.test.ts`, D11 função complexa `.ts`, D12 dependência outward `.ts`)
- `evals/canaries_metricas.py` — canários: fence `[]`→2, assertless `.test.ts` bilateral, edges `./helper.js`→`.ts`, churn commit só-binário
- `tests/test_metric_gates.py` — cobertura dos novos caminhos

Editados (v3 — correção Goldfish round-2, veredito REJECT sobre b988b88):
- `docs/designs/portoes-metricos-ub.md` — esta revisão
- `henke/metricas/polyglot.py` — (a) `_looks_like_lifetime` só em lang `rs` e `'x'`/`'\x'` exige `'` de fechamento, nunca `\n`; (b) `_mask_template` trata strings/`//`/`/* */`/templates aninhados dentro de `${}` e guarda `j+1<n`; (c) `find_functions` aceita Go `(T, error)`, TS genérico `<T>`, Rust `match`, e `lang` normalizado sem ponto; (d) `_TESTCALL_RE` com lookbehind `(?<![.\w$])`, encadeadores `runIf`/`skipIf`, callback = último arg, corpos `{...}` ou arrow concisa, ignora `.todo`
- `henke/metricas/assertless_scan.py` — `expect` exige matcher real (não-mock) após `_paren_end`; `throw`/`assert.fail`/`expect.fail` contam como asserção; SWALLOWED isenta `catch(e){x=e}` com `x` asserido fora e não duplica achados; Rust: `.unwrap(`/`.expect(` não são asserção
- `henke/repo_signals.py` — `TEST_RE` aplicado a `"/"+rel` (não ao path absoluto) e cobre `test_*.py`/`_test.py`; edges: `Path(str(base)+e)` antes de `with_suffix`, Rust `use`/módulo-pai, Python `from pkg import mod` tenta submódulo primeiro; flags `--coverage-json`/`--lcov`; match de coverage por sufixo com fronteira `/` sem fallback cego de basename; `churn` subprocess `encoding="utf-8"`; `sys.stdin` reconfigurado
- `henke/metricas/crap_score.py` — `sys.stdin` reconfigurado UTF-8
- `henke/metricas/ratchet.py` — idem
- `henke/metricas/tornado_scan.py` — idem
- `henke/metricas/fence_check.py` — idem
- `henke/install_skills.py` — instala `henke/repo_signals.py` + `henke/metricas/` junto às skills
- `tests/test_install.py` — cobre a instalação dos scripts
- `henke/spec_drift.py` — `re.findall` extrai múltiplos paths por bullet
- `henke-code-cirurgico/SKILL.md` — ratchet com `--root` escopado aos arquivos tocados (dívida legada não dispara BUDGET-BUST)
- `henke-arch-pastas/SKILL.md` — corrige invocação `repo_signals.py churn`
- `PROTOCOLO.md` — §10/linha 172 reflete SKILLs alterados
- `README.md` — linha 159 idem
- `evals/canaries_metricas.py` — canários: aspas simples TS, template aninhado, Go `(int, error)`, TS `<T>`, Rust `match`/`use`, `./tela.service`→`.ts`
- `tests/test_metric_gates.py` — cobertura dos novos caminhos

Fora de escopo (declarado): `prompts/`, `.henke/`, nenhum arquivo do `virusclip` (verificação física é read-only).

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
- `python henke/validate_skills.py` → verde (regressão)
- `python tests/test_evals.py` + `python evals/canaries.py` → verde (regressão)

## 7. O que NÃO será verificado

- mutant-hunt (sem runtime de mutação nesta spec) → permanece `[A CONSTRUIR]`.
- Cobertura por função em TS: loaders aceitos agregam por arquivo; per-function em JS exige instrumentação fora do stdlib — declarado.
- Dispatch automático de coverage: o coletor aceita `--coverage-json`/`--lcov`, mas quem produz o relatório é a toolchain do projeto (vitest/coverage.py/go test) — `[PROSA]` no SKILL.
- Scanners léxicos (não-AST) são aproximações O(N): generics TS com `<`/`>` aninhados, macros Rust complexos e regex literals ambíguos podem deslocar fronteiras de função — erro admissível declarado, sem crash e sem falso-verde.
- closed-stream/lane-check aplicados aos portões **antigos** (hardening retroativo — P2 futuro).
