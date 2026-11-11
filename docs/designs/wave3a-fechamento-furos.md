# Spec: Wave 3a — Fechamento de Furos dos Portões & Alinhamento da Suíte

**Spec-hash binding:** este documento é a spec de referência formal da Wave 3a para validação via `spec_drift.py` e `contract_closure.py`.

## 1. Contexto & Problema

Na auditoria adversarial da Suíte Henke (Wave 2.5), identificou-se furos de segurança, assimetrias e falsos-positivos nos portões mecânicos e no roteador:
1. **Contrabando de supressão no `hazard_scan`**: supressões `hazard-ok` eram extraídas de linhas raw, permitindo que uma string literal (`s = "hazard-ok: SHELL-INJECTION — motivo"`) suprimisse hazards reais na linha seguinte; além disso, `hazard-ok` sem regra explícita aplicava um `ALL` implícito perigoso.
2. **Máscara inadequada de código vs. dados**: regras puramente estruturais de código (`SHELL-INJECTION`, `UNSAFE-DESERIALIZE`, `ELECTRON-INSECURE-CONFIG`) inspecionavam strings em Python, gerando falso-positivo na docstring do próprio `hazard_scan.py` e impedindo o dogfooding limpo (`exit 0`).
3. **Escopo global de bypass JWT e top-level cego**: `jwt.verify` em qualquer lugar do arquivo isentava chamadas inseguras em outras funções; código fora de funções (top-level) não era verificado pelas 5 regras intraprocedurais.
4. **Cálculo de paths em subpastas no `_check_env_committed`**: caminhos de arquivos commitados falhavam quando o alvo era uma subpasta em vez da raiz do Git.
5. **Vacuous Pass no `contract_closure`**: seção `## 5. Pontes` vazia ou com linhas malformadas passava com `exit 0` silencioso; títulos com variações ("Ponte de Contrato") não eram aceitos.
6. **Evasão no `coverage_ledger`**: ledger com `audited_units == 0` aprovava 100% de cobertura; unidades podiam ser massivamente marcadas como `out_of_scope` sem limite.
7. **Roteador com vetos incompletos e fallback zerado**: comandos destrutivos imperativos não disparavam `EXECUTAR` com `requires_human_auth`; vocabulário bilíngue (EN) ausente; fallback sob `confidence == "none"` zerava todos os portões em vez de manter os portões mínimos de segurança; `CARPINTEIRO` não incluía `spec_drift.py`.
8. **Dogfooding & Versão desalinhada**: `hazard_scan.py .` falhava no próprio repositório da suíte; `PROTOCOLO.md` permanecia em v3.3 enquanto `README.md` já declarava v3.4.

## 2. Decisões Técnicas & Arquitetura

1. **`polyglot.py`**:
   - Função `find_comment_spans(text: str, lang: str = "") -> list[CommentSpan]` extrai `(start_line, start_col, end_line, end_col, text)`.
   - Suporte a comentários HTML `<!-- ... -->` para `.vue`, `.svelte` e `.html`.
   - `mask_py(text: str)` para mascarar strings e comentários em Python via `tokenize` stdlib mantendo posições e quebras de linha.

2. **`hazard_scan.py`**:
   - `_extract_suppressions`: extrai `hazard-ok` ESTRITAMENTE de comentários reais. Proíbe supressão dentro de strings.
   - Banir `ALL` implícito: exige `hazard-ok: RULE — motivo`. Falta de regra ou motivo < 3 chars dispara `UNJUSTIFIED-SUPPRESSION`.
   - Regras de código (`SHELL-INJECTION`, `UNSAFE-DESERIALIZE`, `ELECTRON-INSECURE-CONFIG`) rodam sobre código mascarado (`masked`).
   - Regras de dados/segredos (`HARDCODED-SECRET`, `SQL-CONCAT`, `CORS-CRED-WILDCARD`, `IMDS-V1`, `PROTO-POLLUTION`, etc.) rodam sobre texto com strings preservadas (`no_comments`).
   - Intraprocedural estendido: inclui escopo `<top-level>` para as 5 regras intraprocedurais.
   - JWT: isenção de `jwt.verify` restrita ao corpo da função/bloco em análise.
   - `_check_env_committed`: resolve `git rev-parse --show-toplevel`.
   - Dogfooding: repo roda com `exit 0`. Fixtures adversariais em `evals/` são registradas em `evals/hazard_fixtures_manifest.json` com vigilância contra entradas órfãs.

3. **`contract_closure.py`**:
   - Regex de título flexível: `^## 5\..*?Ponte.*$`.
   - Se a seção existir mas 0 pontes forem encontradas: erro `EMPTY-BRIDGES` (exit 2).
   - Se houver linhas começando com `-` que não casam com `_BRIDGE_RE`: erro `MALFORMED-BRIDGE-SYNTAX` (exit 2).

4. **`coverage_ledger.py`**:
   - Se `audited_units == 0`: erro `ZERO-AUDITED` (exit 1).
   - Teto configurável `--max-out-of-scope-pct` (default 20.0%): se `out_of_scope / total > 0.20`, erro `EXCESSIVE-OUT-OF-SCOPE` (exit 1).

5. **`henke_router.py`**:
   - Destrutivos (`delete`, `deleta`, `drop`, `truncate`, `wipe`, `force-push`, `reset --hard`, etc.) disparam `requires_human_auth = True`.
   - Destrutivos imperativos classificam como `EXECUTAR`.
   - Perguntas preservam `ANALISAR`.
   - Vocabulário bilíngue PT/EN expandido.
   - Quando `confidence == "none"`, portões mínimos de segurança são fornecidos: `["henke/repo_signals.py", "henke/metricas/hazard_scan.py"]`.
   - `CARPINTEIRO` inclui `henke/spec_drift.py`.

## 3. Contratos de Tipos, Entrada & Saída

- `polyglot.CommentSpan`: `NamedTuple("CommentSpan", [("start_line", int), ("start_col", int), ("end_line", int), ("end_col", int), ("text", str)])`
- `polyglot.find_comment_spans(text: str, lang: str = "") -> list[CommentSpan]`
- `hazard_scan.py` exit codes: `0` (limpo), `1` (hazards encontrados), `2` (erro de uso / stdin fechado).
- `contract_closure.py` exit codes: `0` (ok), `1` (pontes quebradas / arquivo ausente), `2` (spec malformada / vazia / ponte malformada).
- `coverage_ledger.py verify` exit codes: `0` (ok), `1` (cobertura insuficiente / zero auditado / excessive out-of-scope / stale / unmapped), `2` (erro de schema / raiz inválida).

## 4. Escopo Declarado

- `docs/designs/wave3a-fechamento-furos.md`
- `henke/metricas/polyglot.py`
- `henke/metricas/hazard_scan.py`
- `henke/metricas/contract_closure.py`
- `henke/metricas/coverage_ledger.py`
- `henke/henke_router.py`
- `evals/hazard_fixtures_manifest.json`
- `evals/canaries_metricas.py`
- `tests/test_metric_gates.py`
- `PROTOCOLO.md`
- `.github/workflows/ci.yml`

## 5. Pontes de Contrato

- ponte: `henke/metricas/polyglot.py::find_comment_spans -> henke/metricas/hazard_scan.py`
- ponte: `henke/metricas/hazard_scan.py::scan_path -> evals/canaries_metricas.py`
- ponte: `henke/metricas/contract_closure.py::check_contract_closure -> evals/canaries_metricas.py`
- ponte: `henke/metricas/coverage_ledger.py::verify_ledger -> evals/canaries_metricas.py`
- ponte: `henke/henke_router.py::route_request -> tests/test_metric_gates.py`
- ponte: `henke/henke_router.py::"henke/spec_drift.py" -> tests/test_metric_gates.py`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos (Estadista)

1. **Contrabando de Supressão (`[MECÂNICO: canário M52]`):** Injeção de `hazard-ok` dentro de string literal não deve suprimir comandos perigosos.
2. **Supressão Cega Sem Regra (`[MECÂNICO: canário M53]`):** `hazard-ok` genérico sem identificador de regra dispara `UNJUSTIFIED-SUPPRESSION`.
3. **Falso-Positivo em Docstring / Fixture (`[MECÂNICO: canário M54]`):** Docstrings e strings em Python mascaradas não acionam regras de código.
4. **Isolamento de Escopo JWT (`[MECÂNICO: canário M55]`):** Presença de `jwt.verify` em uma função do arquivo não isenta função insegura no mesmo arquivo.
5. **Top-Level Inseguro (`[MECÂNICO: canário M56]`):** Código fora de funções é inspecionado para todas as 5 regras intraprocedurais.
6. **Vacuous Pass em Pontes (`[MECÂNICO: canário M57]`):** Seção `## 5` vazia ou malformada sai com código 2.
7. **Cobertura Fantasma (`[MECÂNICO: canário M58]`):** Ledger com 0 unidades auditadas ou excesso de `out_of_scope` sai com código 1.
8. **Destrutivo e Bilíngue no Roteador (`[MECÂNICO: canário M59]`):** Comandos destrutivos exigem `requires_human_auth` e classificam em `EXECUTAR` ou `ANALISAR` conforme a intenção gramatical.
9. **Portões Mínimos e CARPINTEIRO com spec_drift (`[MECÂNICO: canário M60]`):** Roteador sob confiança nula fornece portões mínimos de integridade, e CARPINTEIRO exige `spec_drift.py`.
10. **Vigilância de Manifesto Órfão & Dogfooding (`[MECÂNICO: canário M61]`):** Nenhuma entrada pode existir em `hazard_fixtures_manifest.json` sem correspondência física em teste e dogfooding limpo no repo (`hazard_scan.py .` exit 0).

## 7. Ledger Esperado de Comandos

- `python henke/metricas/contract_closure.py --spec docs/designs/wave3a-fechamento-furos.md --repo .`
- `python henke/metricas/hazard_scan.py .`
- `python evals/canaries_metricas.py`
- `python -m unittest discover -s tests -p "*.py"`

## 8. O que NÃO será verificado

- Análise interprocedural entre arquivos remotos em repositórios externos.
- Execução ativa de exploits em produção (todas as provas são locais em localhost/fixtures).
