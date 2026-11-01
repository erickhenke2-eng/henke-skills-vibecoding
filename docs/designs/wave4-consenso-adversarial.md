# Wave 4 — Consenso Adversarial: Metaconfig & Anti-TOCTOU, Anti-Broker IPC, Anti-Diluição de Cobertura

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py`. Alterá-la após aprovação invalida a aprovação.

## 1. Contexto & Problema

Três vetores de contorno que nenhum portão atual cobre, consolidados num consenso adversarial:

1. **Metaconfig como bypass de portão (TOCTOU)**: portões auditam código-fonte, mas `package.json.scripts` (pretest/test/posttest/prepack/postinstall), `pyproject.toml`, `Cargo.toml`, `build.rs`, `conftest.py`, workflows e `.cargo/config` podem ser mutados entre a auditoria e o publish — executando código não auditado no pipeline. `npm test` é tratado como string cega em `audit_ledger.py`, sem resolver o grafo de hooks (o teste real pode estar num `pretest`, ou um `posttest` pode destruir evidência). Bundles compilados em `dist/` nunca são re-escaneados — segredos/endpoints podem entrar no artefato final.
2. **Anti-broker IPC & ação oculta**: `contract_closure` prova que o literal do canal existe nas duas pontas, mas uma literal morta plantada (`const x = "APP_STATE_SYNC"`) fecha a ponte sem callsite real; canais computados (`atob`, `join`, template interpolado) e dispatch por indexação cega (`handlers[x]`, `obj[k]`, `getattr`) escondem ações não declaradas; switch sem `default` permite ação silenciosa.
3. **Diluição de cobertura**: o teto global de `out_of_scope` pode ser contornado concentrando OOS numa única pasta (código perigoso no `core/` diluído por DTOs auditáveis na `ui/`); arquivo criado na própria PR pode ser marcado `out_of_scope`; teste com `try/catch` vazio ou `.catch(() => {})` sem re-throw nem asserção ("hit-and-swallow") não é reprovado.

## 2. Decisões Técnicas & Arquitetura

1. **`scripts/metricas/metaconfig_diff.py`** (novo):
   - CLI: `--repo <dir> --against <ref> [--spec <spec>] [--json]`.
   - Alvos protegidos: `package.json`, `pyproject.toml`, `Cargo.toml`, `build.rs`, `conftest.py`, `.github/workflows/*`, `.cargo/config(.toml)`.
   - Inspeção semântica: `package.json` compara `scripts`, `dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies`, `overrides`, `resolutions`, `publishConfig`, `bin` (diff por chave); TOML compara seções de dependências/build-system/scripts; `build.rs`, `conftest.py`, workflows e `.cargo/config` tratam qualquer diff (incl. arquivo novo/deletado) como mutação.
   - Autorização: arquivo listado em `## 4. Escopo Declarado` da spec fornecida via `--spec` é autorizado; qualquer outra mutação → `METACONFIG-MUTATION`, exit 1, saída JSON com `"requires_human_auth": true`.
   - Exit codes: `0` limpo, `1` mutação não autorizada, `2` erro de uso/ref inválida.

2. **`scripts/audit_ledger.py`**: função `resolve_npm_lifecycle(cmd: str, pkg: dict) -> list[tuple[str, str]]` resolve o grafo de hooks npm (`npm test`/`pnpm test`/`yarn test` → `pretest`, `test`, `posttest`; `npm run X` → `preX`, `X`, `postX`), com guarda de ciclo. Cada comando do grafo passa pelas mesmas proibições do ledger (publish, etc.). Ativado via flag `--package-json <path>`.

3. **`scripts/metricas/hazard_scan.py`**:
   - Flag `--dist <dir>`: escaneia diretório de build (extensões `.js`, `.mjs`, `.cjs`, `.map`, `.html`, `.json`), teto de 8MB/arquivo (oversize continua fail-closed), mesmo pipeline de regras por classe de máscara.
   - Regra `FRAME-INTROSPECTION` (classe código): `sys._getframe`, `inspect.stack(`, `inspect.currentframe`, `Error.prepareStackTrace`, `new Error(...).stack` parsing.
   - Regra `DYNAMIC-DISPATCH-IPC` (intraprocedural): corpo de função com registro IPC (`ipcMain|ipcRenderer|handle|invoke|channel`) + indexação dinâmica `ident[ident]` sem literal → flag.

4. **`scripts/metricas/contract_closure.py`** (v2):
   - Sintaxe estendida: `- ponte: <origem>::"<CANAL>" -> <destino> [ações: "a1", "a2"]`.
   - `CHANNEL-LITERAL-DEAD`: literal do canal precisa aparecer como argumento direto de callsite (`…("CANAL"` ou `…,'CANAL'` dentro de chamada) no arquivo de origem; literal em declaração solta não fecha.
   - `COMPUTED-CHANNEL`: callsite IPC cujo primeiro argumento é `atob(`, `join(`, `.concat(`, template literal com `${` → reprovado.
   - `[ações: …]`: cada literal de ação deve aparecer no destino; ausente → `BROKEN-BRIDGE-ACTION` (exit 1).
   - `HIDDEN-DISPATCH`/`NO-DEFAULT-DENY`: no destino, handler registrando o canal com indexação dinâmica (`handlers[x]`, `obj[k]`, `getattr`) → flag; `switch` sem `default:` → flag.

5. **`scripts/metricas/coverage_ledger.py`**:
   - `--max-out-of-scope-per-dir-pct` (default 20.0): teto OOS por diretório-pai (dirs com ≥3 unidades); excesso → `DILUTED-OUT-OF-SCOPE` exit 1.
   - `--against <ref>`: arquivo com status `A` no diff marcado `out_of_scope` → `NEW-FILE-OUT-OF-SCOPE` exit 1.

6. **`scripts/metricas/assertless_scan.py`**: `try`/`catch`/`except` (JS/TS/Py) e `.catch(())` sem `expect|assert|throw|pytest.raises|rejects|raise` no bloco → `HIT-AND-SWALLOW`.

7. **`scripts/henke_router.py`**: `scripts/metricas/metaconfig_diff.py` entra no conjunto de portões do `CARPINTEIRO`.

## 3. Contratos de Tipos, Entrada & Saída

- `metaconfig_diff.py`: `check_metaconfig(repo: Path, against: str, spec: Path|None) -> tuple[list[str], bool]` retorna `(mutações, requires_human_auth)`. Exit `0/1/2`.
- `audit_ledger.py`: `resolve_npm_lifecycle(cmd: str, pkg: dict) -> list[tuple[str, str]]`.
- `contract_closure.py`: `check_contract_closure(...) -> tuple[bool, list[str]]` — novos erros `CHANNEL-LITERAL-DEAD`, `COMPUTED-CHANNEL`, `BROKEN-BRIDGE-ACTION`, `HIDDEN-DISPATCH`, `NO-DEFAULT-DENY`.
- `hazard_scan.py`: novas regras `FRAME-INTROSPECTION`, `DYNAMIC-DISPATCH-IPC`; flag `--dist <dir>`.
- `coverage_ledger.py verify`: flags `--max-out-of-scope-per-dir-pct`, `--against`; novos erros `DILUTED-OUT-OF-SCOPE`, `NEW-FILE-OUT-OF-SCOPE`.
- `assertless_scan.py`: nova regra `HIT-AND-SWALLOW`.

## 4. Escopo Declarado

- `docs/designs/wave4-consenso-adversarial.md`
- `scripts/metricas/metaconfig_diff.py`
- `scripts/metricas/hazard_scan.py`
- `scripts/metricas/contract_closure.py`
- `scripts/metricas/coverage_ledger.py`
- `scripts/metricas/assertless_scan.py`
- `scripts/audit_ledger.py`
- `scripts/henke_router.py`
- `evals/canaries_metricas.py`
- `PROTOCOLO.md`

## 5. Pontes de Contrato

- ponte: `scripts/metricas/metaconfig_diff.py::check_metaconfig -> evals/canaries_metricas.py`
- ponte: `scripts/audit_ledger.py::resolve_npm_lifecycle -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/contract_closure.py::check_contract_closure -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/coverage_ledger.py::verify_ledger -> evals/canaries_metricas.py`
- ponte: `scripts/metricas/assertless_scan.py::scan_path -> evals/canaries_metricas.py`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos (Estadista)

1. **metaconfig_diff sem git**: `--against` com repo sem histórico → exit 2 limpo, nunca traceback.
2. **package.json com JSON inválido em qualquer lado do diff**: comparação cai para diff textual da seção — mutação continua detectada.
3. **Ciclo de hooks npm** (`preX` chama `npm run X`): guarda de conjunto visitado, máx. profundidade 8.
4. **dist/ fora de repo git**: `_check_env_committed` já tolera ausência de git; `--dist` nunca exige repo.
5. **Literal do canal em comentário**: mascarado antes da busca de callsite — comentário não fecha ponte.
6. **Dir com 1-2 unidades**: isenta do teto por-diretório (evita falso-vermelho trivial); documentado.
7. **`catch` com asserção**: `catch(e){ expect(e).toBe(...) }` aprova — só catch sem asserção/re-throw reprova.
8. **Anti-loop**: 1ª falha → diagnóstico; 2ª → método diferente; 3ª → mudar de rota.

## 7. Ledger Esperado de Comandos

- `python evals/canaries_metricas.py` → M64+ vermelhos antes dos fixes; 100% verde depois.
- `python scripts/metricas/hazard_scan.py .` → exit 0 (dogfooding).
- `python scripts/spec_drift.py --expected-hash <hash> --against <spec-commit>` → exit 0.
- `python scripts/metricas/contract_closure.py --repo . --spec docs/designs/wave4-consenso-adversarial.md --require-touched` → exit 0.
- `python tests/test_metric_gates.py`, `python scripts/validate_skills.py` → verde.

## 8. O que NÃO será verificado

- Semântica real de execução dos scripts npm (resolução é declarativa, não executa).
- `.map` files: escaneados como texto — `sourceContent` embutido não é parseado campo a campo.
- Regras IPC em linguagens fora JS/TS/Py (Rust `match` dinâmico etc.) — regex é aproximação declarada.
- Cobertura per-dir por módulos de domínio reais (usa diretório-pai filesystem como proxy).
- Merge da branch na main — feito pelo dono após contraprova cega do Gemini.
