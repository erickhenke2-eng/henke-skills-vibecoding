# MAPA DA CODEBASE — SUÍTE HENKE (58 SKILLS)

## 1. Visão Geral da Arquitetura
Repositório central de governança, memória e automação para desenvolvimento assistido por IA (vibecoding disciplinado). Distribui 58 macro-skills compatíveis com a especificação aberta Agent Skills (`SKILL.md`) para múltiplos agentes (Codex, Claude Code, Gemini CLI, Cursor, Devin, Antigravity).

## 2. Estrutura de Diretórios & Função de Cada Pasta

- `scripts/`: Portões mecânicos, CLI global, roteador determinístico, máquina de estados e instaladores em Python puro (`henke_cli.py`, `run_state.py`, `henke_gate.py`, `spec_drift.py`, `audit_ledger.py`, `henke_router.py`, `repo_signals.py`, `validate_skills.py`, `install_skills.py`).
- `scripts/metricas/`: Bateria de portões métricos assimilados (`hazard_scan.py`, `assertless_scan.py`, `contract_closure.py`, `a11y_scan.py`, `metaconfig_diff.py`, `coverage_ledger.py`, `mutant_hunt.py`, `fence_check.py`, `ratchet.py`, `crap_score.py`, `tornado_scan.py`) e a biblioteca de extração de comentários `polyglot.py`. No total a suíte expõe **14 portões executáveis** despacháveis via `henke gate` (`GATE_MAP` de `scripts/henke_cli.py`): os 10 portões métricos registrados + `henke_gate.py`, `spec_drift.py`, `audit_ledger.py` e `repo_signals.py`.
- `tests/`: Suíte de 56 testes automatizados com Python `unittest` cobrindo instaladores, validação de YAML, detecção de drift e portões mecânicos.
- `evals/`: Cenários e baterias de avaliação comportamental, canários bilaterais (M1-M80) e benchmark adversarial (D1-D24 + C1-C4).
- `prompts/`: Catálogo de prompts mestres de orquestração universal (ex: Mega-Sprint 360 com checkpoint ou autopilot).
- `docs/designs/`: Diretório reservado para Design Specs vinculadas a hash SHA-256 no padrão Google EGM.
- `.henke/`: Memória persistente, cofre de decisões, diário de bordo e baseline de custódia do projeto.
- `.github/workflows/`: Pipelines de CI/CD para validação automática de pull requests e integridade de skills.

### Categorias das 58 Macro-Skills (Diretórios `henke-*` na raiz):
- `henke-skills`: Orquestrador mestre e Chief of Staff (roteamento dinâmico em 5 fases).
- `henke-prepara`: Bootstrap de governança, criação de `.henke/` e mapa estrutural.
- `henke-plan-*` (6 skills): Concepção, radar, ideias, MVP, fluxos e histórias de usuário.
- `henke-mem-*` (5 skills): Diário, retomada, decisões, mapa da codebase e faxina de zumbis.
- `henke-arch-*` (7 skills + `henke-blindagem` = 8 skills): Stack, pastas 3 gavetas, desmembramento, monorepos, banco SQLite/Postgres, segurança estrutural, Multi-Cloud AWS/GCP/Cloudflare e blindagem.
- `henke-ui-*` (7 skills): Anti-AI-slop visual, paleta semântica, tipografia, estados, micro-interações, responsividade e auditoria visual.
- `henke-code-*` (10 skills): Pontes IPC/rotas, contratos formais (Zod/TypeBox/Pydantic/OpenAPI), mocks, edição cirúrgica, formulários, autenticação, integrações, comentários úteis, cenas 3D e integrações seguras de LLM/RAG/Tool-Calling.
- `henke-debug-*` (7 skills): Debug backend, frontend, raiz, console, rede, sanidade e reversão segura.
- `henke-audit-*` (6 skills): Auditoria adversarial de vibecoding, varredura de segurança/segredos, Red Team/pentest local, dossiê determinístico de cobertura por SHA-256, auditoria forense de performance e banca adversarial multi-modelo (consenso com quorum).
- `henke-test-*` (1 skill): Caça a testes fracos e falsos-verdes por mutação cirúrgica de AST (`mutant_hunt.py`).
- `henke-refactor-*` (2 skills): Caça a código duplicado e renomeação semântica.
- `henke-ops-*` (4 skills): Gestão de `.env`, preflight de produção, observabilidade estruturada/telemetria e deploy controlado com trava PUBLISH.

## 3. Arquivos de Entrada e Governança Vitais
- `PROTOCOLO.md`: A constituição operacional v4.0 definindo os portões mecânicos e regras de custódia.
- `README.md`: Documentação geral da suíte e manual de instalação.
- `scripts/henke_cli.py`: CLI global unificada `henke` (`init`, `doctor`, `route`, `exec`, `check`, `gate`, `approve`, `scope`, `goldfish`) — dona do `GATE_MAP` com os 14 portões executáveis.
- `scripts/run_state.py`: Máquina de estados executável de `.henke/runs/<run_id>.json`, derivando `outcome` (`verified|failed|blocked`) dos portões reais.
- `scripts/henke_gate.py`: Portão mecânico de baseline e custódia física contra mutações indevidas.
- `scripts/spec_drift.py`: Portão verificador de integridade de escopo e hash da spec.
- `scripts/validate_skills.py`: Validador de conformidade sintática e semântica das 58 skills.
- `install.ps1` / `install.sh`: Instaladores rápidos para Windows e Unix.
