# Design Spec — Correções da Auditoria Completa (AUD-01..AUD-05)

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py` e do `contract_closure.py`. Alterá-la após aprovação invalida a aprovação.

## 1. Contexto & Problema

Auditoria completa (SHERLOCK, ledger em `$TEMP/henke-audit-ledger.json`, 73/73 unidades) encontrou achados corrigíveis autorizados pelo mantenedor ("continue a auditoria e ja corrija assim que terminar"):

- **AUD-02 (divergência de env):** `cli_exec.py` mantinha strip-list própria divergente de `state_paths.sanitized_child_env` — `NODE_*`, `PSModulePath`, `ComSpec`, `PATHEXT`, `CDPATH`, `CARGO_*`, `GEM_HOME`, `BUNDLE_*` escapavam pela rota `henke exec`.
- **AUD-03 (cobertura não medida):** `crap_score` operava sob `cov=0%` presumido — o pipeline nunca media cobertura de linha real.
- **AUD-05 (ledger stale):** `docs/audit/coverage_ledger.json` descrevia árvore pré-refactor (`scripts/`).
- **AUD-06 (higiene):** `scratch/` vazio + `scratch_suite.log` na raiz.

## 2. Decisões Técnicas & Arquitetura

1. **Fonte única de env:** `cli_exec` delega a `sanitized_child_env(repo_root)` e aplica apenas os extras da rota exec (`PYTEST_*`, `COVERAGE_*`, `PWD`, `OLDPWD`, `RUBYLIB`, `CLASSPATH`) — união estrita das duas políticas, nunca interseção. Zero regressão de strip: todo prefixo removido antes continua removido (verificado por diff das listas).
2. **Cobertura no CI:** a mesma bateria `unittest discover` passa a rodar sob `coverage run --include="henke/*"`, seguida de `coverage report` + `coverage json -o cov.json` (formato aceito por `repo_signals --coverage`, alimentando `crap_score` com dados reais).
3. **`coverage>=7.4,<8`** entra em `requirements.txt` (usado só no CI; runtime continua PyYAML-only via `pyproject.toml` — requirements não é o manifesto do pacote).
4. **Ledger regenerado** pós-correção, descrevendo a árvore atual.

## 3. Contratos de Tipos, Entrada & Saída

- `sanitized_child_env(repo_root: Path) -> dict[str,str]` — contrato existente em `state_paths.py`, inalterado.
- `_cmd_exec` mantém assinatura e formato de saída do exec-log; apenas a política de env do filho muda (mais estrita).
- `cov.json` — artefato de CI no formato `coverage json` (compatível com `repo_signals --coverage`).

## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)

<!-- spec_drift.py exige touched ⊆ declared; contract_closure.py exige declared ⊆ existing -->
- `henke/cli_exec.py`
- `.github/workflows/ci.yml`
- `requirements.txt`
- `docs/audit/coverage_ledger.json`
- `docs/designs/auditoria-fixes-2025.md`
- `tests/test_isoboot_guard.py`

## 5. Pontes de Contrato

<!-- contract_closure.py valida cada ponte (modo identificador ou modo string/IPC entre aspas) -->
- ponte: `henke/state_paths.py::sanitized_child_env -> tests/test_deepseek_regressions.py`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos (Estadista)

1. **Queda de Energia / Crash Parcial** (`[PROSA]`): correções são diffs de código/CI, sem escrita transacional nova; ledger regenerado via `coverage_ledger init` (escrita atômica própria do tool).
2. **Escala 100x / Efeito Tornado** (`[PROSA]`): `coverage` adiciona ~30-50% de overhead na bateria — aceitável dentro do `timeout-minutes: 25` do job (bateria roda em ~6 min hoje).
3. **Direção de Dependência (3 Gavetas)** (`[MECÂNICO: fence_check.py]`): `cli_exec` importa `state_paths` — kernel/utilitário, sem ciclo (state_paths só importa `os`/`pathlib`).
4. **Evolução & Migração de Estado** (`[PROSA]`): env mais estrito pode remover variáveis que um filho legado esperasse — mitigado: todas as variáveis novas strippadas são loader-hooks/canary-vars, nunca dados de aplicação.
5. **Fronteira de Falsificabilidade** (`[MECÂNICO: canários bilaterais]`): `tests/test_exec_contract.py` + `test_deepseek_regressions.py` provam que o env sanitizado continua funcional; união das listas é verificável por inspeção estática do diff.

## 7. Ledger Esperado de Comandos

- `python henke/spec_drift.py --spec docs/designs/auditoria-fixes-2025.md --against HEAD`
- `python henke/metricas/metaconfig_diff.py --repo . --against HEAD --spec docs/designs/auditoria-fixes-2025.md`
- `python henke/metricas/hazard_scan.py .`
- `python henke/metricas/assertless_scan.py tests`
- `python -m pytest tests/test_deepseek_regressions.py tests/test_exec_contract.py tests/test_state_paths.py -x -q`

## 8. O que NÃO será verificado

- Rotação da credencial OAuth do GitHub em `mcp_config.json` (ação externa ao repositório, fora da custódia git).
- Execução real de `henke auto --driver <cli>` com CLI de agente externo instalado.
- Kill rate de `mutant_hunt` neste diff (sem mutantes elementares aplicáveis nas linhas alteradas — a mudança é delegação para helper já testado).
