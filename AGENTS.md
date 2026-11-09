# Instruções Operacionais para Agentes de IA (`AGENTS.md`)

Este arquivo é lido automaticamente por agentes de inteligência artificial (**Devin**, **Claude Code**, **Google Antigravity**, **Codex**, **Cursor** e equivalentes) ao operar dentro deste repositório ou ao utilizar a Suíte Henke.

O cumprimento das diretrizes abaixo é **obrigatório e verificado mecanicamente por portões determinísticos**. Qualquer tentativa de trapaça teatral, falso-verde ou desvio de escopo resultará em `exit 1` nos portões.

---

## 1. Princípios Fundamentais (PROTOCOLO v4.0 — 58 Skills)

1. **A Regra dos Dois Agentes (Elefante vs. Peixinho Dourado & Banca Adversarial):**
   - Nunca use o mesmo agente para implementar código e se auto-aprovar no mesmo prompt.
   - O **Agente Construtor** opera focado no diff cirúrgico e nos testes unitários.
   - O **Agente Auditor** (ou **Banca Adversarial** via `henke-audit-banca`) opera em contexto limpo (fresh context), sem viés de confirmação, executando testes de forma independente e adversarial.

2. **Proibição Absoluta de Vitória Falsa (Anti-Glazing):**
   - É estritamente proibido afirmar "todos os testes passaram" ou "código 100% verificado" sem executar fisicamente os comandos no terminal via `henke exec -- <cmd>` (gravando `stdout_sha256` em `.henke/exec_log.jsonl`).
   - É proibido criar testes com mocks circulares que só verificam se spies foram chamados sem asserir o resultado (`assertless_scan.py`) ou que deixam mutantes elementares sobreviverem no diff (`mutant_hunt.py`).
   - É proibido engolir exceções em blocos `try/catch` para forçar testes a ficarem verdes (`HIT-AND-SWALLOW`).
   - É proibido mascarar falhas visuais ou de acessibilidade com truques como `<span class="sr-only">` duplicando texto visível (`a11y_scan.py`).

3. **Seção Obrigatória "O que NÃO foi verificado":**
   - Todo relatório de entrega DEVE conter explicitamente o que ficou de fora (ex: endpoints externos, testes E2E com GPU real, variáveis de ambiente de produção).
   - O script `python scripts/metricas/coverage_ledger.py render-gaps <ledger.json>` gera essa seção de forma mecânica e padronizada.

---

## 2. Máquina de Estados: Os 3 Modos de Operação

Antes de executar qualquer ação, determine o modo da sessão (registrado em `.henke/runs/<run_id>.json` por `scripts/run_state.py`):

| Modo | Quando Usar | Permissões de Escrita | Portões Obrigatórios |
|---|---|---|---|
| **`ANALISAR`** | Perguntas, diagnóstico, auditoria, banca adversarial, caça a bugs, investigação. | **NENHUMA**. Proibido alterar qualquer arquivo do projeto auditado. Arquivos temporários/ledger devem ir para `$TEMP`. | `repo_signals.py`, `hazard_scan.py`, `assertless_scan.py`, `a11y_scan.py`, `coverage_ledger.py` |
| **`PLANEJAR`** | Concepção, desenho de arquitetura, histórias, MVP. | **APENAS** em `docs/designs/` e `.henke/`. Proibido alterar código-fonte (`src/`, `packages/`). | `TEMPLATE.md`, `spec_drift.py`, `contract_closure.py`, `fence_check.py` |
| **`EXECUTAR`** | Implementação cirúrgica, correção de bugs, integrações LLM, observabilidade, testes. | **APENAS nos arquivos declarados** em `## 4. Escopo Declarado` da spec aprovada. | `spec_drift.py`, `contract_closure.py`, `ratchet.py`, `assertless_scan.py`, `mutant_hunt.py`, `metaconfig_diff.py`, `run_state.py` |

> ⚠️ **Trava de PUBLISH:**  
> Ações destrutivas ou de publicação (`deploy`, `push --force`, remoção em massa de arquivos, `drop table`, `rollback`) exigem **autorização humana prévia e explícita** com alvo e comando confirmados.

---

## 3. Os 3 Arquétipos Operacionais & Especializações v4.0

Ao receber uma tarefa, assuma a postura do arquétipo correto:

* **`SHERLOCK` (Auditoria, Banca Adversarial, Performance & Investigação Forense):**
  - Use ferramentas estáticas e de cobertura para mapear 100% dos arquivos antes de opinar (`henke-audit-cobertura`, `henke-audit-banca`, `henke-audit-perf`, `henke-test-mutantes`).
  - Siga a cadeia do sink até o source com prova física de `file:line`.
  - Nunca atribua severidade a hipóteses não comprovadas (`needs_validation` é estritamente proibido de ter `severity`).
* **`CARPINTEIRO` (Construção Cirúrgica, Contratos, LLMs & Observabilidade):**
  - Aplique a menor alteração suficiente (Minimal Diff) (`henke-code-cirurgico`, `henke-code-contratos`, `henke-code-llm`, `henke-ops-observa`).
  - Feche contratos entre todas as pontas (Frontend $\leftrightarrow$ Preload $\leftrightarrow$ Backend $\leftrightarrow$ Banco de Dados $\leftrightarrow$ Saída estruturada de LLM).
  - Nunca esqueça canais literais de IPC, rotas HTTP (`ROUTE:METHOD`) ou exports/imports declarados (`contract_closure.py`).
* **`ESTADISTA` (Planejamento de Longo Prazo):**
  - Isole camadas de responsabilidade: UI nunca importa executores de I/O (`fence_check.py`).
  - Preveja os 5 horizontes de falha na Matriz de Pré-Mortem antes de gerar código.

---

## 4. Portões de Verificação Obrigatórios & CLI Unificada (`henke`)

Antes de submeter qualquer commit ou concluir sua tarefa, execute os portões pertinentes (via `scripts/henke_cli.py` ou diretamente):

```bash
# 0. CLI Unificada (rota, execução com hash-log e bateria de portões)
python scripts/henke_cli.py route --prompt "<sua tarefa>" --repo .
python scripts/henke_cli.py exec --repo . -- <comando_de_teste>
python scripts/henke_cli.py check --repo . --spec docs/designs/<sua-feature>.md

# 1. Roteamento e análise inicial de escopo
python scripts/henke_router.py --prompt "<sua tarefa>" --repo .

# 2. Varredura de riscos de segurança em código, IaC e CI/CD (não altera nada)
python scripts/metricas/hazard_scan.py .

# 3. Verificação contra testes teatrais e cheats de acessibilidade (.sr-only)
python scripts/metricas/assertless_scan.py <diretório_de_testes>
python scripts/metricas/a11y_scan.py .

# 4. Testes de mutação cirúrgica no diff (caça a testes fracos)
python scripts/metricas/mutant_hunt.py --target <arquivo.py> --diff --test-cmd "<cmd_teste>"

# 5. Verificação de deriva de escopo (exige spec aprovada)
python scripts/spec_drift.py --repo . --spec docs/designs/<sua-feature>.md

# 6. Verificação de fechamento bilateral de contrato e pontes
python scripts/metricas/contract_closure.py --repo . --spec docs/designs/<sua-feature>.md --require-touched

# 7. Inicialização e verificação de cobertura (para auditorias)
python scripts/metricas/coverage_ledger.py init --root . --out $TEMP/ledger.json
python scripts/metricas/coverage_ledger.py verify $TEMP/ledger.json --min-pct 90

# 8. Inspeção de metaconfiguração, lockfiles, untracked e anti-TOCTOU
python scripts/metricas/metaconfig_diff.py --repo . --against main

# 9. Verificação de custódia, baseline git e manifesto de execução (run_state)
python scripts/henke_gate.py check --repo .
python scripts/run_state.py verify --run .henke/runs/<run_id>.json

# 10. Auditoria do ledger executável, exec_log.jsonl e expansão de lifecycle
python scripts/audit_ledger.py --ledger .henke/LEDGER.json --require-exec-log
```

Se qualquer um desses scripts retornar **código diferente de 0**, a tarefa **NÃO ESTÁ PRONTA**. Corrija a raiz da falha em vez de tentar contornar a verificação.

---

## 5. Protocolo de Sentinels de Sessão (Wave 7)

Sessões de agente podem registrar sua custódia via `henke session`:

- `henke session start --agent <nome> --workspace <dir> --run-id <id>` cria um sentinel em `<state_dir>/sessions/<session-id>.sentinel.json` com schema, sessão, run, agente, workspace, PID do dono (diagnóstico apenas — nunca autoridade para matar), timestamps e workspaces registrados.
- `henke session heartbeat <session-id>` renova a janela de 1 hora do sentinel.
- `henke session end <session-id>` marca `state=closed`.
- `henke session gc` reapa somente sentinels expirados e invoca o GC efêmero seguro; nunca remove sentinels vivos.
- O PID do dono é informativo: nenhum fluxo do Henke encerra processos por PID. A supervisão é por Job Object (Windows) ou process group (POSIX) do `ChildSupervisor`.
- `henke watch` é exclusivamente foreground/interativo; o hook de pre-commit sempre roda `check --staged --no-cache --full` e nunca usa atalhos de watch ou manifesto.
