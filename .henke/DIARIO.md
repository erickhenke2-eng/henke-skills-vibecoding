# DIÁRIO DE BORDO VIVO — SUÍTE HENKE

## [2026-10-30 00:41] — Inicialização de Governança e Setup de Memória (Dogfooding Henke)
- **Autor:** Engenheiro de Execução Suíte Henke
- **Fase:** Setup & Baseline de Custódia (Fase 1 / Mega-Sprint 360)
- **Ações Realizadas:**
  1. Registrado baseline de custódia formal com `scripts/henke_gate.py baseline` (Branch: `main`, HEAD: `cdbdc142`, 0 arquivos sujos).
  2. Inicializada estrutura de memória `.henke/` com `CONFIG.json`, `MAPA.md`, `DECISOES.md` e `DIARIO.md`.
  3. Criado diretório `docs/designs/` para acolher as especificações Google EGM vinculadas por hash SHA-256.
  4. Executada suíte de testes com Python standard library `unittest`: 29 testes passaram em 43.8s com 0 falhas.
  5. Validada estrutura das 49 skills com `scripts/validate_skills.py`: 49/49 skills em conformidade estrutural.
- **Próximos Passos:**
  - Realizar varredura de Raio-X Forense 360 (segurança, dependências, código morto, linting, testes).
  - Apresentar o Quadro de Saúde Inicial (Nota 0-10) antes de qualquer intervenção no código.

## [2026-11-09] — Registro Retroativo + Sprint da Máquina (Conductor)
- **Autor:** Engenheiro de Execução Suíte Henke
- **Fase:** Consolidação Waves 2→5 + Auditoria R1 da Máquina (registro retroativo)
- **Ações Realizadas:**
  1. **Wave 2** — Roteador determinístico `scripts/henke_router.py` e portões métricos assimilados (`hazard_scan`, `coverage_ledger`, `contract_closure`), com a Trindade Operacional formalizada (spec `docs/designs/wave2-cloudflare-nvidia-tob.md`).
  2. **Wave 2.5** — Catálogo expandido para 53 skills (`audit-pentest`, `arch-cloud`, `audit-cobertura`, `code-contratos`) e `hazard_scan` cobrindo IaC Multi-Cloud AWS/GCP/Cloudflare (spec `docs/designs/wave25-pentest-multicloud-53skills.md`).
  3. **Wave 3a** — Fechamento de furos de evasão dos portões e dogfooding limpo do `hazard_scan` no próprio repositório (spec `docs/designs/wave3a-fechamento-furos.md`).
  4. **Wave 4** — Consenso adversarial: `metaconfig_diff.py`, resolução real do lifecycle npm no `audit_ledger` (anti-TOCTOU), pontes (canal, ação) no `contract_closure` e regra `HIT-AND-SWALLOW` (spec `docs/designs/wave4-consenso-adversarial.md`).
  5. **Wave 5** — PROTOCOLO v4.0: CLI global `henke` (`scripts/henke_cli.py`), máquina de estados `scripts/run_state.py`, portões `a11y_scan` e `mutant_hunt`, catálogo em 58 skills (spec `docs/designs/wave5-banca-consenso-v4.md`).
- **Auditoria R1 (hoje):**
  1. Recebidos 3 relatórios da perícia da máquina em `auditorias-henke/r1-maquina-2026-11-09`: 22 portões verdes, 2 achados 🔴 e 6 🟡 estruturais, 20 fricções mapeadas e 12 propostas de melhoria da máquina.
  2. Iniciada a construção do "modo máquina" (conductor) a partir das propostas levantadas pela R1.
- **Próximos Passos:**
  - Endereçar os 2 🔴 e 6 🟡 estruturais da R1 e consolidar o "modo máquina" em spec própria.
  - Manter `MAPA.md`/`DIARIO.md`/`DECISOES.md` sincronizados com o inventário real: 58 skills, 14 portões executáveis e PROTOCOLO v4.0.

### Sprint da Máquina — entrega (mesma data, fechamento)
- **Itens executados (6 paralelos, workers + verificação do conductor):**
  1. **router-pack (M5):** `henke_router.py` com flexão PT segura (gênero só em finais adjetivais — canário M45 preservado), verbos de publicação e imperativos em `EXEC_SIGNALS` (deploy→EXECUTAR+auth), fallback de engenharia por modo; corpus `evals/router_corpus.jsonl` (147 linhas) + `tests/test_router_corpus.py`.
  2. **detect-module (M4):** `scripts/henke_detect.py` (`detect_project`/`write_project` → `.henke/PROJECT.json`) + 13 testes.
  3. **gates-runner (M3):** `scripts/henke_gates.py` (`run_gates` mecânico, `MAX_REWORK=2`, record via `run_state`) + 14 testes.
  4. **docs-custody:** MAPA/DIARIO/DECISOES/prompts/CI sincronizados à realidade (58 skills, 14 portões, M1-M80, D1-D24+C1-C4, v4.0); emenda ADR-001-E1 (PyYAML só no validador não instalado).
  5. **cli-pack (M6+M7+M9+M10+M11+A2):** `henke auto|setup|run|report|status`, `exec --timeout`, `check --staged` implementado, `goldfish` honesto (envelope ou `goldfish_unavailable`, nunca check disfarçado), `bin/henke`+`bin/henke.cmd` entregues pelo instalador, README corrigido (14 portões, 14 subcomandos).
  6. **machine-core (M1+M2+M8):** `scripts/henke_auto.py` — pipeline route→detect→policy→envelope(SKILL.md verbatim+sha256)→driver→gates(rework≤2)→test_cmd→verify; publish/destrutivo SEMPRE exige `henke approve` humano.
- **Evidência:** 109 testes OK; canários 80/80; bench 23/23+4/4; smoke real em fixture (`henke setup`+`auto --driver echo`: pipeline completa, outcome `failed` honesto por portão exit 2; publish bloqueado exit 2 com instrução de approve); run `sprint-maquina-2026-11-09` derivada `verified`.
- **Fixes do conductor:** `--json` no `route` (contrato do auto), filtro `.envelope.json` na listagem de runs, `henke-audit-banca/SKILL.md` atualizado p/ novo `goldfish`.
- **Pendente (decisão do dono):** skill zumbi `henke-plan-thumb` instalada em 3 destinos fora do manifesto — remoção manual pendente.

## [2026-11-10] — Auditoria adversarial externa (SWE-2) + correções de fechamento
- **Autor:** Devin (SWE-2), modo SHERLOCK→CARPINTEIRO
- **Auditoria (prova física, não alegação):**
  - `pytest tests`: 577 passaram, 7 skipped (POSIX-only). canários 5/5 + 84/84 + 15/15 wave7; bench strict 23/23; hazard/assertless/a11y dogfooding 0 achados.
  - `python -m unittest discover -s tests -p "test_*.py"` (comando oficial do CI/README) **quebrava com ImportError** em HEAD: 5 testes novos inserem `scripts/` no sys.path, colidindo `tests/test_impact.py` com `scripts/test_impact.py`. 11 commits após o último CI verde (`be979a9`) nunca tinham rodado essa linha.
- **Correções aplicadas:**
  1. `tests/test_impact.py` → `tests/test_impact_selection.py` (fim da colisão; discover volta a rodar).
  2. Removidos 65 guards `HAS_*`/`skipTest("Milestone not implemented")` mortos dos e2e (módulos todos existem; skip viraria falso-verde se um módulo sumir) + flags em `e2e_common.py`.
  3. README: canários 80→84 (2 lugares); `TEST_READY.md` (fóssil apontando p/ worktree w6, "66 awaiting" mentiroso) **deletado**.
  4. `PROTOCOLO §7`: fallback caveman embutido quando nenhuma skill de compressão é invocável; `henke-skills §3` aponta pra ele. Promessa "economia de tokens" deixa de depender de skills que o repo não distribui.
  5. `ci.yml`: removida a 2ª execução duplicada do `unittest discover` (~5min/job × 6 jobs economizados).
  6. `MAPA.md`: M1-M80→M1-M84, C1-C4→C2-C4, contagem de testes e lista de subcomandos da CLI sincronizados à realidade.
- **NÃO fechado (dívida declarada):**
  - Pacote wheel instala top-level `scripts/` em site-packages (colisão com outras libs de mesmo nome); correção = renomear para `henke/` — cirurgia grande, adiada. `pipx` (recomendado) isola e não sofre.
  - `henke_cli.py` (2222 linhas), `henke_auto.py` (2157), `run_state.py` (1749) acima do limiar de 500 do próprio desmembrador — tarefa planejada separada, não drive-by (PROTOCOLO §7).
  - Zumbi `henke-plan-thumb` em `~/.agents` e `~/.claude` continua pendente de remoção manual (pré-manifesto, instalador não remove por design).
  - 130 `except: pass` em `scripts/` — auditados todos (2026-11-10, segunda varredura): fail-closed (erro→exit 2, cache-miss, fallback de encoding) ou best-effort declarado; nenhum falso-verde.

## 2026-11-10 — Rename scripts/→henke/ + desmembramento henke_cli (tasks 1-3 aprovadas)

- **Task 3 (zumbi):** `henke-plan-thumb` em ~/.agents MANTIDA — domínio único (thumbnails/retention, integra VirusClip §8), não é duplicata do catálogo, custo zero parada. Decisão: não apagar.
- **Task 1 (rename):** `scripts/`→`henke/` mecânico (241 arqs). Wheel agora expõe só `henke/` — colisão de site-packages eliminada. Armadilhas corrigidas: `from scripts import` (8), campo npm `"scripts"` corrompido→`"henke"` (9: henke_detect, audit_ledger, metaconfig_diff, canaries, e2e_common, test_detect), var `scripts.get` → `henke.get` (4), specs de `docs/designs/` (fixtures vivas). `docs/audit/` segue congelado (dossier datado c/ hashes).
- **Task 2 (desmembrar):** `henke_cli.py` 2222L → 8 módulos por responsabilidade: `cli_kernel`(78), `cli_base`(106), `cli_exec`(555), `cli_check`(475), `cli_runs`(254), `cli_ops`(152), `cli_pipeline`(318), `henke_cli`(459+re-exports compat). DAG: kernel←{exec→ops, check, runs→pipeline, base}←cli. Bootstrap `_PKG_ROOT` preservado p/ execução como script (respawn isoboot).
- **Fix re-aplicado (estava perdido sem commit):** `check --json` → `is_interactive = ... and not as_json`.
- **NÃO desmembrado (decisão documentada):** `run_state.py` — um único trabalho (lifecycle de runs); `henke_auto.py` — helpers ~20L, o problema real é `_run_auto_inner` (1011L = uma orquestração): decompor exige stage-context, trilha própria.
- **Verificado:** unittest discover OK; pytest ~577; e2e 79/79; canaries 5/5+84/84+15/15; validate 58; hazard/assertless 0; wheel build+install+doctor+`check --json` limpo.
- **plan-thumb:** movida de `~/.agents/skills` + `~/.claude/skills` → `F:/DEV/ACTIVE/virusclip/.agents/skills/henke-plan-thumb` (cópia idêntica verificada por diff). Repo nunca a referenciou — a poluição era no catálogo global instalado. Zumbi eliminado.

## 2026-11-10 — Desmembramento completo (<500L) de `run_state.py`, `henke_auto.py` e `cli_exec.py`

- **`run_state.py` (1749L → 6 módulos <500L):** `rs_io`(227L: atomic fsync + `_file_lock` + paths), `rs_custody`(341L: tokens/tips/manifestos/sidecars em `~/.config/henke/`), `rs_git`(126L: snapshots git higienizados), `rs_verify`(488L: `derive_run_outcome` + `verify_run_file`), `rs_mutations`(404L: `init_run`/`record_gate`/`amend_scope`/`approve_run`), `run_state`(306L: CLI + ISO-BOOT + bind anti-PYTHONPATH A6 + re-exports).
- **`henke_auto.py` (2157L, max_complexity 314 → 7 módulos <450L, max_complexity 57):** `auto_kernel`(310L), `auto_policy`(279L), `auto_driver`(331L), `auto_custody`(296L), `auto_audit`(285L), `auto_gates`(424L), `henke_auto`(442L: orquestrador de 8 estágios + seams preservadas para `mock.patch.object` + ISO-BOOT + re-exports).
- **`cli_exec.py` (555L → 2 módulos <400L):** `cli_exec_proc`(208L: parser de test runners + supervisão de árvore) + `cli_exec`(362L: hash-chain `exec_log.jsonl` + `LEDGER.json`).
- **Verificado:** 22 módulos das 3 famílias 100% <500L; `validate_skills` 58 OK; `hazard_scan` 122 arqs / 0 achados; `assertless_scan` 584 testes / 0 achados; `unittest discover`, `pytest tests/e2e`, canários (5/5, 84/84, 15/15), `bench_corpus --strict` e wheel build+install verdes.
