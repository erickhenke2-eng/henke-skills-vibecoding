# MAPA DA CODEBASE — SUÍTE HENKE (49 SKILLS)

## 1. Visão Geral da Arquitetura
Repositório central de governança, memória e automação para desenvolvimento assistido por IA (vibecoding disciplinado). Distribui 49 macro-skills compatíveis com a especificação aberta Agent Skills (`SKILL.md`) para múltiplos agentes (Codex, Claude Code, Gemini CLI, Cursor, Devin, Antigravity).

## 2. Estrutura de Diretórios & Função de Cada Pasta

- `scripts/`: Portões mecânicos e instaladores em Python puro (custódia git, spec drift, auditoria de ledger, validação de schema e instalador seguro com rollback).
- `tests/`: Suíte de testes automatizados com Python `unittest` cobrindo instaladores, validação de YAML, detecção de drift e portões mecânicos.
- `evals/`: Cenários e baterias de avaliação comportamental e benchmarks de agentes.
- `prompts/`: Catálogo de prompts mestres de orquestração universal (ex: Mega-Sprint 360 com checkpoint ou autopilot).
- `docs/designs/`: Diretório reservado para Design Specs vinculadas a hash SHA-256 no padrão Google EGM.
- `.henke/`: Memória persistente, cofre de decisões, diário de bordo e baseline de custódia do projeto.
- `.github/workflows/`: Pipelines de CI/CD para validação automática de pull requests e integridade de skills.

### Categorias das 49 Macro-Skills (Diretórios `henke-*` na raiz):
- `henke-skills`: Orquestrador mestre e Chief of Staff (roteamento dinâmico em 5 fases).
- `henke-prepara`: Bootstrap de governança, criação de `.henke/` e mapa estrutural.
- `henke-plan-*` (6 skills): Concepção, radar, ideias, MVP, fluxos e histórias de usuário.
- `henke-mem-*` (6 skills): Diário, retomada, decisões, mapa da codebase e faxina de zumbis.
- `henke-arch-*` (6 skills + `henke-blindagem`): Escolha de stack, pastas 3 gavetas, desmembramento, monorepos, banco SQLite/Postgres e segurança estrutural.
- `henke-ui-*` (7 skills): Anti-AI-slop visual, paleta semântica, tipografia, estados, micro-interações, responsividade e auditoria visual.
- `henke-code-*` (8 skills): Pontes IPC/rotas, mocks, edição cirúrgica, formulários, autenticação, integrações, comentários úteis e cenas 3D.
- `henke-debug-*` (6 skills): Debug backend, frontend, raiz, console, rede e reversão segura.
- `henke-audit-*` (2 skills): Auditoria adversarial de vibecoding e varredura de segurança/segredos.
- `henke-refactor-*` (2 skills): Caça a código duplicado e renomeação semântica.
- `henke-ops-*` (3 skills): Gestão de `.env`, preflight de produção e deploy controlado com trava PUBLISH.

## 3. Arquivos de Entrada e Governança Vitais
- `PROTOCOLO.md`: A constituição operacional v3.1.1 definindo os portões mecânicos e regras de custódia.
- `README.md`: Documentação geral da suíte e manual de instalação.
- `scripts/henke_gate.py`: Portão mecânico de baseline e custódia física contra mutações indevidas.
- `scripts/spec_drift.py`: Portão verificador de integridade de escopo e hash da spec.
- `scripts/validate_skills.py`: Validador de conformidade sintática e semântica das 49 skills.
- `install.ps1` / `install.sh`: Instaladores rápidos para Windows e Unix.
