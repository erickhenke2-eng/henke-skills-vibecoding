# Galeria de Prompts Mestres — Suíte Henke

Este diretório contém os **Prompts de Orquestração Prontos para Uso** da Suíte Henke, desenhados para serem colados diretamente em agentes autônomos (Devin, Claude Code, Codex, Antigravity, Cursor, etc.).

Cada prompt encadeia as 49 skills da suíte com portões de custódia, disciplina operacional e verificações de evidência física.

---

## Catálogo de Prompts

| Arquivo | Nome do Prompt | Objetivo | Quando Usar |
|---|---|---|---|
| [`01-mega-sprint-360-universal.md`](./01-mega-sprint-360-universal.md) | **Mega-Sprint 360 Universal** | Varredura profunda do repositório (Sanidade, Vibecode, Segurança, Runtime/Memória, UI/UX), parada obrigatória para decisão humana e execução cirúrgica pós-aprovação. | Quando você quer que o agente audite um repositório inteiro, ache problemas invisíveis, apresente um menu de decisões de 1 clique e aplique as correções autorizadas com portões de segurança. |

---

## Como Usar com Qualquer Agente

1. Abra o agente na raiz do repositório desejado (ex: `devin`, `claude`, `codex`).
2. Copie o conteúdo do arquivo `.md` correspondente ao seu objetivo.
3. Cole na conversa inicial do agente.
4. O agente executará as etapas de auditoria de forma autônoma e **parará no Checkpoint Humano** para que você aprove as decisões e features antes de qualquer alteração de código.
