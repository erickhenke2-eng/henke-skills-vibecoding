# Spec EXECUTAR — Correções da Banca Adversarial v17

## 1. Objetivo

Aplicar as correções dos achados confirmados pela banca adversarial
(3 auditores independentes, contexto limpo, prova file:line) e persistir
o protocolo como feature de primeira classe (`henke banca`).

## 2. Achados confirmados → correção

| ID | Achado | Correção |
|---|---|---|
| B1 | `cli_exec`: hash-chain hasheava `rc` real mas persistia `exit:124` em timeout → wedge permanente | hash cobre o valor persistido |
| B2 | `coverage_ledger`: `needs_validation`/`finding_confirmed` aceitavam `checks:[{}]` como auditado | valida conteúdo do check igual a `covered` |
| B3 | `audit_ledger._npm_script_name`: wrappers com flag arg separada (`timeout 10`, `sudo -u`, `nice -n`) escondiam o runner | consome args posicionais/flags do wrapper |
| B4 | `event_store`: append estendia cadeia sem verificar; log sem âncora não era flag | verify flag log+sem-âncora; append verifica antes |
| B5 | `rs_io`: `_pid_alive` lia LastError do thread errado; `_stat`/`_raw` unbound | `use_last_error=True`; guards None/"" |
| A1 | `process_runner`: wrap `cmd /c` de builtin sem guarda de metacaractere (test_cmd hostil) | recusa `&|<>` etc. como cli_exec |
| A2 | `henke_driver_agent`: `list2cmdline` deixava `&%` crus no re-parse do `cmd /c` | `^`-escape nos elementos sem aspas |
| C1 | `cli_exec` POSIX: which-miss com caminho explícito (`./x.sh`) executava binário repo-resident | recusa path contido no repo |
| C2 | `test_isoboot_guard`: darwin não-framework (pyenv/conda) usava layout framework | `sys._framework` decide o scheme |
| C3 | `hazard_scan._SQL_INTERP_PY_RE`: `\b` tornava `%s`/`.format` inalcançáveis | 3 formas explícitas (f-string/.format/%) |
| C4 | `assertless_scan.classify`: asserts em ramos mortos contavam como reais | `_dead_assert_ids` marca unreachable |
| C5 | CI: `coverage report` sem `--fail-under` — medição cosmética | `--fail-under=60` |
| SEC | `spec_drift`/`metaconfig`: filter.* de .gitattributes executava no status; timeout exit 1; wordlist de negação e protected-list incompletos | neutraliza lfs/attr-file, exit 2, wordlist+ protegidos ampliados |

## 3. Nova feature

`henke/cli_banca.py` — `henke banca dossier|envelope|consolidate`:
orquestra as fases mecânicas do Protocolo Goldfish Todos-contra-Todos
(dossiê determinístico, briefs isolados de auditor, `critic_passes` +
`verify --require-critic`). Nunca despacha driver (disciplina goldfish).

## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)

<!-- spec_drift.py exige touched ⊆ declared; contract_closure.py exige declared ⊆ existing -->
- `henke/cli_exec.py`
- `henke/metricas/coverage_ledger.py`
- `henke/metricas/hazard_scan.py`
- `henke/metricas/assertless_scan.py`
- `henke/metricas/metaconfig_diff.py`
- `henke/spec_drift.py`
- `henke/audit_ledger.py`
- `henke/event_store.py`
- `henke/rs_io.py`
- `henke/process_runner.py`
- `henke/henke_driver_agent.py`
- `henke/cli_banca.py`
- `henke/henke_cli.py`
- `tests/test_isoboot_guard.py`
- `.github/workflows/ci.yml`
- `docs/designs/banca-v17-fixes.md`
- `docs/designs/auditoria-fixes-2025.md`

## 5. Pontes de Contrato

<!-- contract_closure.py valida cada ponte -->
- ponte: `henke/cli_banca.py::cmd_banca -> henke/henke_cli.py`

## 6. Casos de Borda & Matriz Pre-Mortem

- **Falso-verde da própria correção:** cada fix foi re-verificado fisicamente
  (timeout→exec ok, `[{}]` rejeitado, `%s`/`.format` casam, dead-branch=0).
- **Residuais needs_validation:** drivers `filter.*` custom em `.git/config`
  (só lfs neutralizado); paridade de aspas em `_INSTR` do driver_agent;
  10 falhas de suite pré-existentes por `pip install .` local (env, não código).
- **Regressão:** suíte alvo roda limpa; hazard_scan repo-wide 0 findings.
