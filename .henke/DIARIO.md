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
