# ORQUESTRAÇÃO MESTRE UNIVERSAL — SUÍTE HENKE (PIPELINE COMPLETO 49 SKILLS)

Você atuará sob a governança da **Suíte Henke** via Maestro central (`henke-skills`), adotando a disciplina **Caveman** (conciso, fatos secos, zero prolixidade) e **Ponytail** (soluções mais simples possíveis, minimalismo cirúrgico, sem over-engineering).

Sua missão neste repositório é executar uma varredura completa na veia do projeto, dividida em **4 Macro-Etapas rigorosas**.

> 🛑 **REGRA DE PARADA OBRIGATÓRIA (CHECKPOINT HUMANO):**
> Você executará as **ETAPAS 1 e 2 de forma 100% autônoma e somente leitura**. 
> Ao concluir a ETAPA 2, você **DEVE PARAR OBRIGATORIAMENTE**. 
> Apresente o Dossiê de Diagnóstico com o **Menu de Decisões de 1 Clique** e o **Radar de Features**. 
> **NÃO altere uma única linha de código produtivo antes de o usuário responder à Etapa 3.**

---

## ETAPA 1: Setup de Memória, Custódia & Governança Local
*Skills acionadas: `henke-prepara`, `henke-mem-mapa`, `henke-mem-diario`, `henke_gate`*

1. **Custódia & Respeito à Governança do Repositório:**
   - Inspecione `git status`, branch e HEAD.
   - **Regra de Worktrees:** Se o projeto possuir governança própria que exija isolamento (ex: Gauntlet ou worktrees em `.worktrees/`), opere dentro da worktree apropriada. Caso contrário, opere no checkout ativo registrando a fotografia de integridade.
   - **Proteção dos Dados do Dono:** É terminantemente proibido deletar, modificar ou limpar stashes pré-existentes, arquivos `.lnk`, atalhos `.bat`/`.sh`, pastas de credenciais locais (`.secrets/`, `.env`) ou logs de sessões anteriores.
   - Se disponível no path, inicialize: `python scripts/henke_gate.py baseline`. Caso contrário, grave formalmente os arquivos sujos/limpos no inventário de memória.
2. **Setup da Memória do Projeto (`henke-prepara`):**
   - Garanta a pasta `.henke/` com `DIARIO.md`, `DECISOES.md` e crie `docs/designs/`.
   - Gere/atualize o mapa estrutural em `.henke/MAPA.md` identificando a árvore essencial e pontos de entrada.

---

## ETAPA 2: Raio-X Forense 360 & Perícia de Camada Zero (Somente Leitura)
*Skills: `henke-audit-vibecode`, `henke-audit-seguranca`, `henke-arch-seguranca`, `henke-debug-sanidade`, `henke-debug-raiz`, `henke-debug-console`, `henke-debug-backend`, `henke-debug-rede`, `henke-debug-frontend`, `henke-ui-auditoria`, `henke-ui-antislop`, `henke-refactor-duplicado`, `henke-arch-desmembrar`*

Investigue a base de código a fundo em 5 eixos físicos:

1. **Sanidade Real de Compilação & Testes (`henke-debug-sanidade`):**
   - Execute o comando oficial de build (`npm run build`, `cargo build`, `vite build`, `tsc`, etc.).
   - Execute a suíte oficial de testes completa.
   - Anote tempos de execução, testes com falhas intermitentes (flaky por timing/concorrência), warnings severos de compilação e tipagem estrita.
2. **Perícia Forense Anti-Gambiarras (`henke-audit-vibecode` & `henke-refactor-duplicado`):**
   - Caça a código morto, imports zumbis e módulos órfãos.
   - Caça a handlers-teatro e stubs que simulam sucesso ou mascaram retornos falsos (`TODO`, `FIXME`, `not implemented`, fallbacks vazios).
   - Identificação de duplicação de regras críticas em múltiplos arquivos.
   - Mapeamento de arquivos gigantes (500+ linhas) candidatos a desmembramento modular (`henke-arch-desmembrar`).
3. **Auditoria de Segurança & Superfície de Ataque (`henke-audit-seguranca` & `henke-arch-seguranca`):**
   - Varredura de credenciais, chaves de API, arquivos `.env` ou tokens commitados.
   - Superfície de injeção: SQL cru, interpolação perigosa de HTML (`innerHTML`/XSS), execução de shell com concatenação de strings sem sanitização.
   - Vulnerabilidades em dependências (`npm audit`, etc.).
4. **Camada de Comunicação, Runtime & Concorrência (`henke-debug-backend` & `henke-debug-rede`):**
   - Análise de chamadas síncronas bloqueando a thread principal (ex: `readFileSync` em eventos de UI/Electron).
   - Vazamentos de memória: listeners de eventos sem cleanup (`removeListener`), URLs de Blob sem descarte (`revokeObjectURL`), buffers pesados trafegados em Base64 no IPC em vez de paths locais.
   - Race conditions em Promises, WebSockets e sincronização de estado.
5. **Auditoria Visual & UX (Se o projeto tiver interface visual) (`henke-ui-auditoria` & `henke-ui-antislop`):**
   - Nota visual de 0 a 10 avaliando clichês de IA (gradientes roxos/neon sem contraste, cards excessivos).
   - Cobertura de estados da tela: carregando, vazio, erro, sucesso e preenchido (`henke-ui-estados`).
   - Armadilhas de acessibilidade e trapaças de `.sr-only`.

---

## ETAPA 3: O GRANDE CHECKPOINT HUMANO (PARADA OBRIGATÓRIA)
*Skills acionadas: `henke-plan-mvp`, `henke-plan-destrava`, `henke-plan-radar`, `henke-mem-decisoes`*

**PARE IMEDIATAMENTE AQUI. NÃO MODIFIQUE CÓDIGO AINDA.**
Apresente ao usuário um relatório de inteligência sintético e objetivo no seguinte formato exato:

### 1. Quadro de Saúde Atual (Nota 0-10)
- **Nota Atual:** `X.X/10`
- **Bugs Críticos & Vazamentos:** (Lista direta com arquivo e linha)
- **Dívidas Técnicas / Flakes:** (O que está instável ou frágil)

### 2. Menu de Decisões de 1 Clique (Escolha para o Usuário)
Apresente alternativas claras no formato:
- **Decisão 1:** [Opção 1A: Descrição sucinta] OU [Opção 1B: Descrição sucinta]
- **Decisão 2:** [Opção 2A: Descrição sucinta] OU [Opção 2B: Descrição sucinta]

### 3. Radar de Futuro — 3 Próximas Features Estratégicas (`henke-plan-destrava` & `henke-plan-radar`)
- **[Feature 1]:** Nome + Proposta de valor + Referência consagrada de mercado.
- **[Feature 2]:** Nome + Proposta de valor + Referência consagrada de mercado.
- **[Feature 3]:** Nome + Proposta de valor + Referência consagrada de mercado.

### 4. Proposta de Escopo da Intervenção Cirúrgica
- Lista exata dos arquivos que serão corrigidos na ETAPA 4.

**Finalize perguntando:**
> *"Quais opções você escolhe no Menu de Decisões e quais correções/features autoriza para a execução imediata?"*

---

## ETAPA 4: Execução Cirúrgica, Validação & Portões (Pós-Autorização)
*(Esta etapa SÓ será iniciada após a resposta e aprovação explícita do usuário)*
*Skills: `henke-plan-historias`, `henke-code-cirurgico`, `henke-blindagem`, `henke-code-ponte`, `henke-arch-stack`, `verification-before-completion`, `henke-ops-preflight`, `henke-mem-diario`*

1. **Design Spec Vinculada (`henke-plan-historias`):**
   - Grave em `docs/designs/sprint-<data>.md` o escopo aprovado, critérios de aceite e a seção obrigatória `## 4. Escopo Declarado` com a lista estrita de arquivos. Calcule o SHA-256 da spec.
2. **Execução com Bisturi Cirúrgico (`henke-code-cirurgico` & `henke-blindagem`):**
   - Aplique o menor diff possível para sanar os problemas autorizados.
   - **Regra `henke-arch-stack`:** Proibido adicionar dependências pesadas de terceiros para resolver problemas solucionáveis com código nativo limpo.
   - Preserve integralmente comentários, docstrings e estruturas não envolvidas.
   - Garanta idempotência, escrita atômica e contratos de tipos estritos (`henke-code-ponte`).
3. **Protocolo Anti-Looping (Rollback de Segurança):**
   - Se qualquer alteração introduzir quebra de build ou falha em testes que antes passavam, e não for sanada em até 2 iterações, reverta o diff do arquivo para o baseline e documente o impedimento. Jamais faça gambiarras em cascata para mascarar quebras.
4. **Portão de Validação & Evidência Física (`verification-before-completion`):**
   - Execute novamente o build completo e a suíte inteira de testes.
   - Verifique que nenhum arquivo fora do escopo foi tocado (`git status --porcelain`).
   - Apresente o ledger comprovando exit 0 de cada comando.
5. **Fechamento e Consolidação:**
   - Grave as decisões tomadas em `.henke/DECISOES.md` e a jornada no `.henke/DIARIO.md`.
   - Entregue o Quadro de Saúde final (**Antes vs Depois**), o bloco *"O que NÃO foi verificado"* e a confirmação com status `verified`.

Inicie a ETAPA 1 imediatamente inspecionando o repositório.
