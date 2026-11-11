# Design Spec — Wave 2: Assimilação Cloudflare + NVIDIA + Trail of Bits & Trindade de Arquétipos

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py` e do `contract_closure.py`. Alterá-la após aprovação invalida a aprovação.

## 1. Contexto

A Wave 1 (`f847f25`) portou 5 portões métricos de qualidade/arquitetura (`assertless_scan`, `crap_score`, `ratchet`, `fence_check`, `tornado_scan`) e coletores poliglotas (`repo_signals.py` + `polyglot.py`). Porém, a auditoria cruzada com `cloudflare/security-audit-skill`, `cloudflare/skills`, `NVIDIA/skills` (`nvidia-skill-finder`) e `trailofbits/skills` (`fp-check`, `sharp-edges`, `audit-context-building`, `differential-review`), validada em dois turnos de alinhamento adversarial com o Devin (`swe-2-max`), identificou 4 lacunas mecânicas:
1. **Descoberta e Roteamento (`[PROSA]` → `[MECÂNICO]`)**: o Maestro escolhia no olhômetro quais das 49 skills ler.
2. **Segurança e Falhas de Runtime (`[PROSA]` → `[MECÂNICO]`)**: `henke-audit-seguranca`, `henke-arch-seguranca` e `henke-blindagem` não tinham scanner determinístico para segredos, injeção de shell/SQL, crypto fraco, path traversal ou concorrência sem semáforo.
3. **Cobertura Auditável de Repositórios Grandes (`[PROSA]` → `[MECÂNICO]`)**: em repos grandes, o agente lia poucos arquivos e declarava auditoria completa sem contabilidade de unidades, sem hash de conteúdo e sem separar `finding_confirmed` de `needs_validation`.
4. **Fechamento de Contrato ("Carpinteiro que não escreve de menos")**: `spec_drift.py` provava `touched ⊆ declared` (não escreveu de mais), mas nada provava `declared ⊆ touched` nem que a ponte `origem::símbolo -> destino` foi realmente ligada.

## 2. Decisões Técnicas (Alinhadas com `swe-2-max`)

- **Zero skills novas (mantém 49 skills)**: os 4 novos mecanismos entram em `henke/` e `henke/metricas/` e são instalados por `install_skills.py`.
- **`henke/henke_router.py` (Padrão NVIDIA `nvidia-skill-finder`)**:
  - Lê os 49 `SKILL.md` como fonte única da verdade (`description` + `## 2. Ponto de Partida` + `when_not:` opcional).
  - Tabela declarativa de desambiguação negativa no topo do script (dados, não `if/elif` espalhado).
  - Opera como veto de Modo: perguntas/ambiguidade → `ANALISAR`; sinais `PUBLISH` (`deploy`, `push --force`, `faxina`, `reversão`, `rm -rf`) → `requires_human_auth: true`.
  - Tiers de confiança (`high | medium | low | none`): quando nenhuma skill se aplica, retorna `skills: []` com `exit 0` (veredito válido, nunca pipeline forçado).
- **`henke/metricas/hazard_scan.py` (2 Passes: `masked` + `raw` sem comentários)**:
  - Pass 1 (`masked` + `find_functions`): `SHELL-INJECTION`, `WEAK-CRYPTO` (escopo de função), `UNBOUNDED-CONCURRENCY`, `UNSAFE-DESERIALIZE`, `PATH-TRAVERSAL` (escopo de função), `XSS-RAW-HTML`, `ELECTRON-INSECURE-CONFIG`.
  - Pass 2 (`raw` com comentários removidos via `tokenize` em `.py` e `mask_comments_only` em C-like): `HARDCODED-SECRET` (ignora exemplos documentais como `AKIAIOSFODNN7EXAMPLE`), `SQL-CONCAT`, `URL-CRED`, mais `ENV-COMMITTED` via `git ls-files`.
  - Supressão vinculada a regra e razão: `hazard-ok: <RULE> — <motivo>` isenta; `hazard-ok` sem motivo dispara `UNJUSTIFIED-SUPPRESSION` (exit 1). Teto de 2 MB por arquivo.
- **`henke/metricas/coverage_ledger.py` (Cloudflare Coverage Ledger + Trail of Bits Dossier & `fp-check`)**:
  - `init`: enumera o universo in-scope com `sha256` por arquivo, `tree_hash` e prioridade de risco (`high|medium|low`) cruzando `edges` (fan-in/entry surfaces) + `metrics` + `hazard_scan`.
  - `verify`: re-percorre o escopo para impedir arquivos `unmapped` ou `stale` (`sha256` mudou → `exit 1`); exige `checks[]` com `assumptions[]: {text, status: enforced|unenforced|unknown}`; exige `trace[]` verificando que `file` existe e `line <= total_lines` em `finding_confirmed`; proíbe severidade em `needs_validation`; rebaixa profile `deep` sem `critic_passes` para `partial`.
  - `render-gaps`: gera mecanicamente o bloco Markdown `#### O que NÃO foi verificado` + resumo de contagens.
- **`henke/metricas/contract_closure.py` + `docs/designs/TEMPLATE.md`**:
  - Verifica `declared ⊆ existing/touched` na seção `## 4. Escopo Declarado` e verifica cada ponte declarada em `## 5. Pontes de Contrato` nos modos identificador (`origem.py::func -> destino.py`) e string/IPC (`origem.ts::"canal:acao" -> destino.ts`).

## 3. Contratos de Saída

Todos os portões seguem a disciplina: `0` = verde, `1` = veredito-vermelho, `2` = não-veredito (fail-closed, entrada malformada ou stdout fechado), com `sys.stdout`/`sys.stderr`/`sys.stdin` reconfigurados para UTF-8.

## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)

- `docs/designs/wave2-cloudflare-nvidia-tob.md`
- `docs/designs/TEMPLATE.md`
- `henke/henke_router.py`
- `henke/metricas/hazard_scan.py`
- `henke/metricas/coverage_ledger.py`
- `henke/metricas/contract_closure.py`
- `PROTOCOLO.md`
- `README.md`
- `henke/install_skills.py`
- `tests/test_install.py`
- `evals/canaries_metricas.py`
- `tests/test_metric_gates.py`
- `evals/bench_corpus.py`
- `henke-skills/SKILL.md`
- `henke-audit-seguranca/SKILL.md`
- `henke-arch-seguranca/SKILL.md`
- `henke-blindagem/SKILL.md`
- `henke-debug-raiz/SKILL.md`
- `henke-code-cirurgico/SKILL.md`
- `henke-code-ponte/SKILL.md`
- `henke-plan-historias/SKILL.md`
- `henke-prepara/SKILL.md`

## 5. Pontes de Contrato

- ponte: `henke/henke_router.py::route_request -> tests/test_metric_gates.py`
- ponte: `henke/metricas/hazard_scan.py::scan_path -> evals/canaries_metricas.py`
- ponte: `henke/metricas/coverage_ledger.py::init_ledger -> evals/canaries_metricas.py`
- ponte: `henke/metricas/contract_closure.py::check_contract_closure -> evals/canaries_metricas.py`
- ponte: `henke/install_skills.py::"henke_router.py" -> tests/test_install.py`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos

1. **Queda de Energia / Estado Parcial**: `coverage_ledger.py` grava o JSON via arquivo `.tmp` + `os.replace` atômico; arquivos alterados após `init` são flagados como `stale` via `sha256`.
2. **Escala 100x (Repositórios Grandes)**: `hazard_scan.py` ignora arquivos > 2 MB (bundles/minified) registrando aviso de não-veredito parcial, e poda `node_modules`, `target`, `dist`, `.git`.
3. **Inversão de Camada**: `hazard_scan`, `coverage_ledger` e `contract_closure` residem em `henke/metricas/` e consomem `polyglot.py` / `spec_drift.py` sem dependência circular.
4. **Evolução de Schema**: `coverage_ledger.py` aceita campos forward-compatible (`attempts`, `prior_status`) sem quebrar.
5. **Bilateralidade**: Canários M34–M45 testam aprovação e reprovação para cada portão novo.

## 7. Ledger Esperado

- `python henke/validate_skills.py` → 49 skills OK
- `python evals/canaries.py` → 5/5 OK
- `python evals/canaries_metricas.py` → 45/45 OK
- `python -m unittest discover -s tests -v` → OK
- `python evals/bench_corpus.py --suite-dir .` → 15/15 defeitos detectados (100%), 2/2 controles aprovados
- `python henke/metricas/contract_closure.py --spec docs/designs/wave2-cloudflare-nvidia-tob.md --repo .` → exit 0

## 8. O que NÃO será verificado

- Execução dinâmica em sandbox OS-level (namespaces/seccomp) durante auditoria: `hazard_scan.py` e `coverage_ledger.py` operam por análise estática determinística.
- Análise de taint interprocedural entre múltiplos arquivos: `WEAK-CRYPTO` e `PATH-TRAVERSAL` operam em escopo intraprocedural (mesmo corpo de função via `polyglot.find_functions`).
