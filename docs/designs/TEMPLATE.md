# Design Spec — [Nome da Feature]

**Spec-hash binding:** este documento é a spec de referência do `spec_drift.py` e do `contract_closure.py`. Alterá-la após aprovação invalida a aprovação.

## 1. Contexto & Problema

[Descreva o problema real e por que esta mudança é necessária]

## 2. Decisões Técnicas & Arquitetura

[Escolhas técnicas, trade-offs e invariantes arquiteturais]

## 3. Contratos de Tipos, Entrada & Saída

[Assinaturas de funções, schemas JSON/TS/Rust/Python e códigos de saída]

## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)

<!-- spec_drift.py exige touched ⊆ declared; contract_closure.py exige declared ⊆ existing -->
- `caminho/para/arquivo.ts`
- `tests/caminho/para/arquivo.test.ts`

## 5. Pontes de Contrato

<!-- contract_closure.py valida cada ponte (modo identificador ou modo string/IPC entre aspas) -->
- ponte: `caminho/para/arquivo.ts::simboloExportado -> tests/caminho/para/arquivo.test.ts`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos (Estadista)

1. **Queda de Energia / Crash Parcial** (`[MECÂNICO]` | `[PROSA]`): [Como evita corrupção de disco/banco — escrita atômica `.tmp` + rename / transação]
2. **Escala 100x / Efeito Tornado** (`[MECÂNICO]` | `[PROSA]`): [Como evita OOM/starvation de GPU/CPU — streaming, paginação, semáforo]
3. **Direção de Dependência (3 Gavetas)** (`[MECÂNICO: fence_check.py]`): [Garantia de que executores não importam UI]
4. **Evolução & Migração de Estado** (`[MECÂNICO]` | `[PROSA]`): [Compatibilidade retroativa de schemas e dados do usuário]
5. **Fronteira de Falsificabilidade** (`[MECÂNICO: canários bilaterais]`): [Qual teste prova o caminho permitido e qual reprova o proibido]

## 7. Ledger Esperado de Comandos

- `python henke/spec_drift.py --spec docs/designs/<este-arquivo>.md --expected-hash <sha256> --against <base>`
- `python henke/metricas/contract_closure.py --spec docs/designs/<este-arquivo>.md --repo .`

## 8. O que NÃO será verificado

- [Liste com transparência hardware, APIs externas ou cenários fora do escopo físico]
