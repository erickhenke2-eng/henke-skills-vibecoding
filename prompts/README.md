# Galeria de Prompts Mestres — Suíte Henke

Este diretório contém os **Prompts de Orquestração Prontos para Uso** da Suíte Henke, desenhados para serem colados diretamente em agentes autônomos (Devin, Claude Code, Codex, Antigravity, Cursor, etc.).

Cada prompt encadeia as 49 skills da suíte com portões de custódia, disciplina operacional e verificações de evidência física.

---

## Catálogo de Prompts Oficiais

| Arquivo | Nome do Prompt | Modo de Execução | Quando Usar |
|---|---|---|---|
| [`01-mega-sprint-360-universal.md`](./01-mega-sprint-360-universal.md) | **Mega-Sprint 360 Universal (Com Checkpoint Humano)** | **Pausa na Etapa 3** | Quando você quer que o agente audite tudo, monte o diagnóstico e **PARE** para te perguntar as decisões arquiteturais e escolhas de features antes de tocar em código. |
| [`02-mega-sprint-360-autopilot.md`](./02-mega-sprint-360-autopilot.md) | **Mega-Sprint 360 Full Autopilot (Sem Parada)** | **Direto de Ponta a Ponta** | Quando você quer colar o prompt, ir dormir ou sair, e deixar o agente auto-aprovar as decisões mais seguras, corrigir os bugs críticos e entregar tudo testado com relatório final. |

---

## Como Usar com Qualquer Agente

1. Abra o agente na raiz do repositório desejado (ex: `devin`, `claude`, `codex`).
2. Copie o conteúdo do arquivo `.md` correspondente ao seu objetivo:
   - Quer validar e escolher as opções no meio do caminho? Use o **`01`**.
   - Quer que ele faça tudo sozinho em uma tacada só? Use o **`02`**.
3. Cole na conversa inicial do agente e acompanhe a execução.
