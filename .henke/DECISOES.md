# COFRE DE DECISÕES ARQUITETURAIS — SUÍTE HENKE

Este documento registra as decisões técnicas fundamentais para impedir que intervenções futuras de agentes desfaçam escolhas aprovadas.

| ID | Data | Contexto / Decisão | Justificativa Técnica | Status |
|---|---|---|---|---|
| ADR-001 | 2026-10-28 | Zero dependências externas pesadas nos scripts de governança (`scripts/`) | `scripts/` deve rodar em qualquer máquina com Python standard library pura (`unittest`, `urllib`, `hashlib`, `json`, `argparse`). | ✅ Ativo |
| ADR-002 | 2026-10-29 | Spec-Hash Binding via `scripts/spec_drift.py` | Garante que nenhuma IA execute código sem Design Spec congelada por hash SHA-256. | ✅ Ativo |
| ADR-003 | 2026-10-29 | Portão de Baseline Físico via `scripts/henke_gate.py` | Impede que stashes, uncommitted changes ou arquivos do usuário sejam tocados sem baseline formal gravado. | ✅ Ativo |
| ADR-004 | 2026-10-30 | Inicialização formal da memória local `.henke/` para Mega-Sprint 360 | O próprio repositório da Suíte Henke deve adotar 100% da sua própria governança (dogfooding rigoroso). | ✅ Ativo |
