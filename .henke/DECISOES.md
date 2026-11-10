# COFRE DE DECISÕES ARQUITETURAIS — SUÍTE HENKE

Este documento registra as decisões técnicas fundamentais para impedir que intervenções futuras de agentes desfaçam escolhas aprovadas.

| ID | Data | Contexto / Decisão | Justificativa Técnica | Status |
|---|---|---|---|---|
| ADR-001 | 2026-10-28 | Zero dependências externas pesadas nos scripts de governança (`scripts/`) | `scripts/` deve rodar em qualquer máquina com Python standard library pura (`unittest`, `urllib`, `hashlib`, `json`, `argparse`). | ✅ Ativo |
| ADR-001-E1 | 2026-11-09 | Emenda à ADR-001 ("stdlib pura"): exceção única — `scripts/validate_skills.py` requer PyYAML (`requirements.txt`) | O validador estrito depende de `yaml.SafeLoader` para rejeitar chaves duplicadas e adulterações de frontmatter. A exceção não viola o espírito da ADR-001: o script roda apenas na suíte e em CI (`install_skills.py` o executa na validação, mas **não o copia para os destinos**), portanto nenhum destino instalado passa a exigir dependência pip. ADR-001 permanece ✅ Ativo com esta ressalva registrada. | ✅ Ativo com ressalva |
| ADR-002 | 2026-10-29 | Spec-Hash Binding via `scripts/spec_drift.py` | Garante que nenhuma IA execute código sem Design Spec congelada por hash SHA-256. | ✅ Ativo |
| ADR-003 | 2026-10-29 | Portão de Baseline Físico via `scripts/henke_gate.py` | Impede que stashes, uncommitted changes ou arquivos do usuário sejam tocados sem baseline formal gravado. | ✅ Ativo |
| ADR-004 | 2026-10-30 | Inicialização formal da memória local `.henke/` para Mega-Sprint 360 | O próprio repositório da Suíte Henke deve adotar 100% da sua própria governança (dogfooding rigoroso). | ✅ Ativo |
