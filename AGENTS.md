# Instruções Operacionais para Agentes de IA (`AGENTS.md`)

Este arquivo é lido automaticamente por agentes de inteligência artificial (**Devin**, **Claude Code**, **Google Antigravity**, **Codex**, **Cursor** e equivalentes) ao operar dentro deste repositório ou ao utilizar a Suíte Henke.

O cumprimento das diretrizes abaixo é **obrigatório e verificado mecanicamente por portões determinísticos**. Qualquer tentativa de trapaça teatral, falso-verde ou desvio de escopo resultará em `exit 1` nos portões.

---

## 1. Princípios Fundamentais (PROTOCOLO v3.4 — 53 Skills)

1. **A Regra dos Dois Agentes (Elefante vs. Peixinho Dourado):**
   - Nunca use o mesmo agente para implementar código e se auto-aprovar no mesmo prompt.
   - O **Agente Construtor** opera focado no diff cirúrgico e nos testes unitários.
   - O **Agente Auditor** opera em contexto limpo (fresh context), sem viés de confirmação, executando testes de forma independente e adversarial.

2. **Proibição Absoluta de Vitória Falsa (Anti-Glazing):**
   - É estritamente proibido afirmar "todos os testes passaram" ou "código 100% verificado" sem executar fisicamente os comandos no terminal.
   - É proibido criar testes com mocks circulares que só verificam se spies foram chamados sem asserir o resultado (`assertless_scan`).
   - É proibido engolir exceções em blocos `try/catch` para forçar testes a ficarem verdes.
   - É proibido mascarar falhas visuais ou de acessibilidade com truques como `<span class="sr-only">` duplicando texto visível.

3. **Seção Obrigatória "O que NÃO foi verificado":**
   - Todo relatório de entrega DEVE conter explicitamente o que ficou de fora (ex: endpoints externos, testes E2E com GPU real, variáveis de ambiente de produção).
   - O script `python scripts/metricas/coverage_ledger.py render-gaps <ledger.json>` gera essa seção de forma mecânica e padronizada.

---

## 2. Máquina de Estados: Os 3 Modos de Operação

Antes de executar qualquer ação, determine o modo da sessão:

| Modo | Quando Usar | Permissões de Escrita | Portões Obrigatórios |
|---|---|---|---|
| **`ANALISAR`** | Perguntas, diagnóstico, auditoria, caça a bugs, investigação. | **NENHUMA**. Proibido alterar qualquer arquivo do projeto auditado. Arquivos temporários/ledger devem ir para `$TEMP`. | `repo_signals.py`, `hazard_scan.py`, `assertless_scan.py`, `coverage_ledger.py` |
| **`PLANEJAR`** | Concepção, desenho de arquitetura, histórias, MVP. | **APENAS** em `docs/designs/` e `.henke/`. Proibido alterar código-fonte (`src/`, `packages/`). | `TEMPLATE.md`, `spec_drift.py`, `fence_check.py` |
| **`EXECUTAR`** | Implementação cirúrgica, correção de bugs, testes. | **APENAS nos arquivos declarados** em `## 4. Escopo Declarado` da spec aprovada. | `spec_drift.py`, `contract_closure.py`, `ratchet.py`, `assertless_scan.py` |

> ⚠️ **Trava de PUBLISH:**  
> Ações destrutivas ou de publicação (`deploy`, `push --force`, remoção em massa de arquivos, `drop table`, `rollback`) exigem **autorização humana prévia e explícita** com alvo e comando confirmados.

---

## 3. Os 3 Arquétipos Operacionais

Ao receber uma tarefa, assuma a postura do arquétipo correto:

* **`SHERLOCK` (Auditoria & Investigação Forense):**
  - Use ferramentas estáticas e de cobertura para mapear 100% dos arquivos antes de opinar.
  - Siga a cadeia do sink até o source com prova física de `file:line`.
  - Nunca atribua severidade a hipóteses não comprovadas (`needs_validation` é estritamente proibido de ter `severity`).
* **`CARPINTEIRO` (Construção & Engenharia Cirúrgica):**
  - Aplique a menor alteração suficiente (Minimal Diff).
  - Feche contratos entre todas as pontas (Frontend $\leftrightarrow$ Preload $\leftrightarrow$ Backend $\leftrightarrow$ Banco de Dados).
  - Nunca esqueça canais literais de IPC ou exports declarados (`contract_closure.py`).
* **`ESTADISTA` (Planejamento de Longo Prazo):**
  - Isole camadas de responsabilidade: UI nunca importa executores de I/O (`fence_check.py`).
  - Preveja os 5 horizontes de falha na Matriz de Pré-Mortem antes de gerar código.

---

## 4. Portões de Verificação Obrigatórios

Antes de submeter qualquer commit ou concluir sua tarefa, execute os portões pertinentes:

```bash
# 1. Roteamento e análise inicial de escopo
python scripts/henke_router.py --prompt "<sua tarefa>" --repo .

# 2. Varredura de 10 riscos de segurança (não altera nada)
python scripts/metricas/hazard_scan.py .

# 3. Verificação contra testes teatrais
python scripts/metricas/assertless_scan.py <diretório_de_testes>

# 4. Verificação de deriva de escopo (exige spec aprovada)
python scripts/spec_drift.py --repo . --spec docs/designs/<sua-feature>.md

# 5. Verificação de fechamento bilateral de contrato e pontes
python scripts/metricas/contract_closure.py --repo . --spec docs/designs/<sua-feature>.md --require-touched

# 6. Inicialização e verificação de cobertura (para auditorias)
python scripts/metricas/coverage_ledger.py init --root . --out $TEMP/ledger.json
python scripts/metricas/coverage_ledger.py verify $TEMP/ledger.json --min-pct 90
```

Se qualquer um desses scripts retornar **código diferente de 0**, a tarefa **NÃO ESTÁ PRONTA**. Corrija a raiz da falha em vez de tentar contornar a verificação.
