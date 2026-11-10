# Wave 5 — Consenso da Banca Adversarial (v4.0): Blindagem de Portões, CLI Global `henke`, 4 Módulos `[A CONSTRUIR]` e 58 Macro-Skills

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py` e do `contract_closure.py`. Alterá-la após aprovação invalida a aprovação.

## 1. Contexto & Problema

A banca adversarial multi-modelo (`devin swe-2-max` $\times$ `opencode/crouton-alpha-free` $\times$ `opencode/beam-free`), fundamentada na auditoria física de 100% das 22 unidades Python do repositório (`coverage_ledger.py verify` = `100.0%`), convergiu em 12 melhorias de alto ROI para elevar a eficácia mecânica da Suíte Henke em 40% a 80%:

1. **Escopo de arquivo inteiro e comentários desativando regras no `hazard_scan.py`**: `_IMDS_SAFE_RE`, `_SANITIZER_RE` e `_SEMAPHORE_RE` verificavam `raw`/`no_comments` no arquivo inteiro (permitindo que um comentário ou outra função desativasse `XSS-RAW-HTML` ou `UNBOUNDED-CONCURRENCY`), e `_XSS_HTML_RE` escaneava `no_comments` (strings preservadas) em vez de `masked`.
2. **Backdoor de manifesto plantado e cegueira de CI/CD no `hazard_scan.py`**: `_get_manifest_exemptions` subia diretórios pais de qualquer arquivo escaneado e usava `.endswith()`, permitindo que um repositório externo plantasse `evals/hazard_fixtures_manifest.json` para silenciar alertas. Além disso, `CONFIG_PATTERNS` não cobria `.github/workflows/*.y*ml` nem `.gitlab-ci.yml`.
3. **Cegueira a arquivos untracked (`??`) e lockfiles no `metaconfig_diff.py`**: `git diff --name-status` ignorava arquivos recém-criados ainda não rastreados (como `conftest.py` ou `build.rs` untracked) e lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `poetry.lock`, `uv.lock`, `Cargo.lock`, `.npmrc`, `Makefile`).
4. **Colisão de substrings e `_touched_files` morto no `henke_router.py`**: sinais como `"check"` e `"review"` faziam match por substring em `"checklist"` e `"preview"`; gatilhos curtos (`"audita"`) puxavam skills de UI em auditorias de backend; verbos compostos (`"audita e corrige"`) ficavam presos em `ANALISAR`; e `scope_paths = _touched_files(repo_root)` (linha 403) era coletado e descartado.
5. **Fricção operacional de 9 scripts dependentes de CWD e `TEMPLATE.md` ausente no `install_skills.py`**: `scripts/` era instalado em `~/.agents/skills/scripts/`, fazendo comandos `python scripts/...` falharem fora do repositório da suíte.
6. **Auto-relato não verificado em `audit_ledger.py`**: `"testes_descobertos": N` era aceito sem vínculo criptográfico com o `stdout_sha256` de um runner real e sem auto-descoberta de `package.json`.
7. **Modo identificador fraco (`\bSym\b`), ausência de rotas HTTP/REST e falta de path containment em `contract_closure.py`**: duas variáveis locais homônimas fechavam ponte sem export/import real; faltava validação `.is_relative_to(repo_root)` e suporte a pontes `ROUTE:METHOD /path`.
8. **4 itens `[A CONSTRUIR]` pendentes no `PROTOCOLO.md`**: máquina de estados `.henke/runs/<run_id>.json` (§2), portão invocável `a11y_scan.py` (§4), runner wrapper `henke exec` (§6) e `mutant_hunt.py` (§10).
9. **5 novas Macro-Skills identificadas pela banca (53 $\to$ 58 skills)**: `henke-audit-banca`, `henke-test-mutantes`, `henke-audit-perf`, `henke-code-llm` e `henke-ops-observa`.

## 2. Decisões Técnicas & Arquitetura

1. **`scripts/metricas/hazard_scan.py` (Ideias 1 e 2)**:
   - `_XSS_HTML_RE` opera sobre `masked` (e não `no_comments`), eliminando falsos-positivos em strings/docstrings.
   - `fn_iter` calcula o fechamento de chaves `{}` sobre `masked` (evitando que `{}` dentro de strings/templates corrompam `el`).
   - `XSS-RAW-HTML`, `UNBOUNDED-CONCURRENCY` e `IMDS-V1` passam a verificar sanitizadores/semáforos/headers intraproceduralmente dentro do span da função (`fn_body`), exigindo que o sanitizador de XSS esteja na mesma expressão RHS ou em offset anterior (`sanitizer_offset < sink_offset`) no mesmo span (ou constante de semáforo no topo do módulo), reportando `<top-level>` / `SCOPE=MODULE` quando aplicável. Flag `--legacy-file-scope` preserva comportamento antigo para backtest.
   - `_get_manifest_exemptions` só honra o manifesto canônico da própria suíte (`_SUITE_ROOT / "evals/hazard_fixtures_manifest.json"`) para arquivos fisicamente contidos em `_SUITE_ROOT` (`p.resolve().is_relative_to(_SUITE_ROOT)`) ou manifesto passado explicitamente via `--fixtures-manifest <path>`, usando igualdade exata de path relativo POSIX em vez de `.endswith()`.
   - `CONFIG_PATTERNS` expandido para `.github/workflows/*.yml`, `.github/workflows/*.yaml` e `.gitlab-ci.yml` com novas regras `CI-PWN-REQUEST` (`pull_request_target` + checkout de PR head), `CI-CURL-PIPE-SH` (`curl ... | sh/bash` sem verificação de hash) e `CI-SECRET-ECHO` (`echo ${{ secrets.* }}`).

2. **`scripts/metricas/metaconfig_diff.py` (Ideia 3)**:
   - `check_metaconfig` combina `git diff --name-status <against>` com `git ls-files --others --exclude-standard` (tratando untracked como `"A"`).
   - Adiciona lockfiles e configs de build (`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `poetry.lock`, `uv.lock`, `Cargo.lock`, `.npmrc`, `Makefile`) aos alvos monitorados.

3. **`scripts/henke_router.py` (Ideia 4)**:
   - Match de sinais em `_PLAN_SIGNALS`, `_EXEC_SIGNALS`, `_AUDIT_SIGNALS`, `_UI_SIGNALS`, etc. usa regex com word-boundary `\b` para sinais de palavra única.
   - Verbos compostos de auditoria + execução (`"audita e corrige"`, `"investiga e conserta"`) classificam em `EXECUTAR` / `CARPINTEIRO` mantendo os portões de diagnóstico.
   - Gatilhos curtos de 1 palavra em `## 2.` (`"audita"`, `"cria"`, `"limpa"`) exigem co-ocorrência de termo da família na janela de `±40` caracteres para pontuar peso cheio.
   - `_touched_files(repo_root)` aplica multiplicador de afinidade `[0.85, 1.25]` quando há arquivos modificados no repositório e expõe `scope_paths` no JSON. Flag `--compact` retorna resumo enxuto para economia de tokens.

4. **`scripts/henke_cli.py`, `pyproject.toml` e `scripts/install_skills.py` (Ideias 5 e 6)**:
   - Binário unificado `henke` (`scripts/henke_cli.py`) resolvendo `SUITE_ROOT` automaticamente e operando sobre `--repo` (default `cwd`).
   - Subcomandos: `init`, `doctor`, `route`, `exec`, `check`, `gate`, `approve`, `scope`, `goldfish`.
   - `henke exec -- <cmd>` executa o comando de teste real, extrai contagem (`pytest`, `unittest`, `vitest`, `jest`, `cargo test`, `go test`), calcula `stdout_sha256`, grava `.henke/exec_log.jsonl` (hash-encadeado) e atualiza `.henke/LEDGER.json`.
   - `pyproject.toml` registra o entrypoint console `[project.scripts] henke = "scripts.henke_cli:main"`, e `install_skills.py` inclui `docs/designs/TEMPLATE.md`, `scripts/henke_cli.py`, `scripts/run_state.py`, `scripts/metricas/a11y_scan.py` e `scripts/metricas/mutant_hunt.py` no inventário instalado.

5. **`scripts/audit_ledger.py` (Ideia 6)**:
   - Auto-descobre `package.json` quando `--package-json` não é informado e `ledger_path` pertence a um repositório com `package.json`.
   - Regex de runner de testes com word-boundary `\b(test|pytest|jest|vitest|mocha|cargo\s+test|go\s+test|unittest)\b`.
   - Função `verify_exec_log(entries, exec_log_path)` e flag `--require-exec-log` verificam que cada entrada de teste possui registro correspondente em `.henke/exec_log.jsonl` com `stdout_sha256` íntegro (`UNVERIFIED_EXECUTION_EVIDENCE` se adulterado).

6. **`scripts/metricas/contract_closure.py`, `scripts/spec_drift.py` e `scripts/henke_gate.py` (Ideias 7 e 8)**:
   - `contract_closure.py`: valida `.is_relative_to(repo_root)` em todas as pontes (`PATH-TRAVERSAL-BRIDGE`); verifica no modo identificador que `src` define/exporta `sym` no topo (e não apenas variável local homônima) e que `dst` importa `src` ou `sym` (`BROKEN-BRIDGE-IMPORT`); suporta pontes HTTP/REST `origem::"ROUTE:METHOD /path/{id}" -> destino` normalizando `:id` $\leftrightarrow$ `{id}` $\leftrightarrow$ `<int:id>`.
   - `spec_drift.py` e `henke_gate.py`: suporte consistente a globs `dir/**` e `dir/*`, ordenação de `discover_latest_spec` por `mtime` (com desempate por nome), rejeição de escopo curinga puro `*` / `**` / `**/*` (`WILDCARD-SCOPE-FORBIDDEN`) e suporte a declarações `arquivo.py::simbolo` em `## 4. Escopo Declarado`.

7. **3 Novos Portões `[A CONSTRUIR]` (`run_state.py`, `a11y_scan.py`, `mutant_hunt.py` — Ideias 8, 9 e 10)**:
   - `scripts/run_state.py`: gerencia `.henke/runs/<run_id>.json`, deriva `outcome` (`verified` | `failed` | `blocked`) mecanicamente a partir dos portões e de `exec_log.jsonl`, rejeita `outcome: verified` manual sem portões (`STATE-DERIVED-CONFLICT`), registra emendas auditadas (`amend_scope`) e aprovação humana (`approve_run`).
   - `scripts/metricas/a11y_scan.py`: portão standalone extraído e expandido de `evals/canaries.py` detectando `A11Y-SR-ONLY-DUPLICATE`, `A11Y-HIDDEN-INTERACTIVE` e `A11Y-EMPTY-CONTROL`.
   - `scripts/metricas/mutant_hunt.py`: motor AST de testes de mutação para Python focado nos arquivos/linhas do diff (`--target`, `--diff`, `--test-cmd`, `--min-kill-rate`, `--max-mutants`), reportando `kill_rate` e sobreviventes (`SURVIVED-MUTANT`).

8. **5 Novas Macro-Skills (Ideias 10, 11 e 12 — 53 $\to$ 58 skills)**:
   - `henke-audit-banca/SKILL.md`: auditoria adversarial multi-modelo (Goldfish todos-contra-todos com quorum $2/3$).
   - `henke-test-mutantes/SKILL.md`: caça a testes fracos via mutação cirúrgica de AST no diff (`mutant_hunt.py`).
   - `henke-audit-perf/SKILL.md`: diagnóstico e blindagem de performance (hotspots CRAP, queries N+1, I/O síncrono em async, benchmark antes/depois).
   - `henke-code-llm/SKILL.md`: engenharia segura de integrações LLM/RAG/Agentes (anti-prompt injection, structured outputs com Zod/Pydantic, timeouts e fallback).
   - `henke-ops-observa/SKILL.md`: observabilidade estruturada, correlação de `run_id`/`trace_id` e eliminação de catches cegos em produção.

## 3. Contratos de Tipos, Entrada & Saída

- `scripts/henke_cli.py`: `main(argv: list[str] | None = None) -> int` (`0` = sucesso, `1` = falha de portão, `2` = erro de uso).
- `scripts/run_state.py`: `derive_run_outcome(run_data: dict) -> tuple[str, list[str]]`, `verify_run_file(run_path: Path) -> tuple[bool, list[str]]`.
- `scripts/metricas/a11y_scan.py`: `scan_a11y_path(target: Path) -> tuple[int, list[tuple[str, str, str]]]` (`(files_scanned, [(rule, loc, detail)])`).
- `scripts/metricas/mutant_hunt.py`: `run_mutant_hunt(target: Path, test_cmd: list[str], max_mutants: int = 15, diff_lines: set[int] | None = None) -> dict`.
- `scripts/audit_ledger.py`: `verify_exec_log(entries: list[dict], exec_log_path: Path) -> list[str]`.

## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)

- `docs/designs/wave5-banca-consenso-v4.md`
- `.gitignore`
- `.henke/CONFIG.json`
- `pyproject.toml`
- `AGENTS.md`
- `PROTOCOLO.md`
- `README.md`
- `scripts/henke_cli.py`
- `scripts/run_state.py`
- `scripts/henke_router.py`
- `scripts/spec_drift.py`
- `scripts/henke_gate.py`
- `scripts/audit_ledger.py`
- `scripts/install_skills.py`
- `scripts/validate_skills.py`
- `scripts/metricas/hazard_scan.py`
- `scripts/metricas/metaconfig_diff.py`
- `scripts/metricas/contract_closure.py`
- `scripts/metricas/assertless_scan.py`
- `scripts/metricas/a11y_scan.py`
- `scripts/metricas/mutant_hunt.py`
- `henke-skills/SKILL.md`
- `henke-audit-banca/SKILL.md`
- `henke-test-mutantes/SKILL.md`
- `henke-audit-perf/SKILL.md`
- `henke-code-llm/SKILL.md`
- `henke-ops-observa/SKILL.md`
- `evals/canaries_metricas.py`
- `evals/bench_corpus.py`
- `tests/test_validator.py`
- `tests/test_metric_gates.py`
- `tests/test_install.py`
- `tests/test_gate.py`

## 5. Pontes de Contrato

- ponte: `scripts/henke_cli.py::main -> tests/test_metric_gates.py`
- ponte: `scripts/run_state.py::verify_run_file -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/a11y_scan.py::scan_a11y_path -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/mutant_hunt.py::run_mutant_hunt -> evals/canaries_metricas.py`
- ponte: `scripts/audit_ledger.py::verify_exec_log -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/hazard_scan.py::scan_path -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/contract_closure.py::check_contract_closure -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/metaconfig_diff.py::check_metaconfig -> evals/canaries_metricas.py`
- ponte: `scripts/henke_router.py::route_request -> tests/test_metric_gates.py`
- ponte: `scripts/henke_router.py::"henke-audit-banca" -> henke-skills/SKILL.md`
- ponte: `scripts/henke_router.py::"henke-test-mutantes" -> henke-skills/SKILL.md`
- ponte: `scripts/henke_router.py::"henke-audit-perf" -> henke-skills/SKILL.md`
- ponte: `scripts/henke_router.py::"henke-code-llm" -> henke-skills/SKILL.md`
- ponte: `scripts/henke_router.py::"henke-ops-observa" -> henke-skills/SKILL.md`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos (Estadista)

1. **Queda de Energia / Crash Parcial (`[MECÂNICO]`):** `run_state.py` e `henke exec` gravam arquivos JSON via escrita atômica (`.tmp` + `os.replace`) e append-only JSONL com hash-chain (`prev_hash` + `entry_hash`).
2. **Escala 100x / Efeito Tornado (`[MECÂNICO]`):** `mutant_hunt.py` limita mutações por default (`--max-mutants 15`) e opera apenas nas linhas tocadas pelo diff, com timeout estrito por execução de subprocesso; `hazard_scan.py` mantém teto de 2MB/arquivo.
3. **Direção de Dependência (`[MECÂNICO: fence_check.py]`):** `scripts/metricas/` não importa `henke_cli.py` nem `run_state.py`; a camada `henke_cli.py` orquestra `scripts/` e `scripts/metricas/` de cima para baixo.
4. **Evolução & Compatibilidade Retroativa (`[MECÂNICO]`):** As 4 specs históricas (`wave2`, `wave25`, `wave3a`, `wave4`) continuam passando 100% em `contract_closure.py`, e todos os canários anteriores (`M1..M70`) continuam verdes.
5. **Fronteira de Falsificabilidade (`[MECÂNICO: canários bilaterais M71..M80]`):** Cada uma das novas regras e portões possui par bilateral (`_fail` reprovado e `_pass` aprovado) em `evals/canaries_metricas.py` e `tests/test_metric_gates.py`.

## 7. Ledger Esperado de Comandos

- `python scripts/validate_skills.py` → 58 skills OK
- `python scripts/metricas/hazard_scan.py .` → 0 findings (exit 0)
- `python scripts/metricas/assertless_scan.py tests evals` → 0 findings (exit 0)
- `python evals/canaries.py` → 5/5 OK
- `python evals/canaries_metricas.py` → 80/80 OK
- `python evals/bench_corpus.py --suite-dir .` → 100% recall e 0 FP
- `python -m unittest discover -s tests -v` → OK
- `python scripts/metricas/contract_closure.py --spec docs/designs/wave5-banca-consenso-v4.md --repo . --require-touched` → exit 0
- `python scripts/spec_drift.py --spec docs/designs/wave5-banca-consenso-v4.md --expected-hash <sha256>` → exit 0

## 8. O que NÃO será verificado

- Mutação semântica profunda em linguagens compiladas fora de Python (`mutant_hunt.py` v1 opera sobre AST stdlib Python; JS/TS/Rust continuam cobertos por `assertless_scan.py` e `crap_score.py`).
- Execução real de pipelines remotos GitHub Actions / GitLab CI (as regras `CI-PWN-REQUEST`, `CI-CURL-PIPE-SH` e `CI-SECRET-ECHO` são verificadas estaticamente sobre os arquivos `.yml`/`.yaml`).
