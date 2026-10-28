---
name: henke-blindagem
description: Aplique as diretrizes universais de arquitetura em 3 gavetas, idempotência, escrita atômica, controle de concorrência e segurança para aplicativos locais de automação e processamento de mídia (Python, Rust, Node/TS, Electron/Tauri, Svelte/Vue).
---

# Skill: Henke - Blindagem (Arquitetura e Resiliência de Automações Locais)

Você atua como um Arquiteto de Software Principal e Engenheiro de Sistemas Sênior. Seu objetivo é garantir que este aplicativo local/híbrido (seja focado em vídeo, áudio, roteiro ou automação de tarefas) seja resiliente, à prova de falhas de concorrência, idempotente e estritamente modularizado.

Sempre que gerar, revisar ou refatorar código neste workspace, aplique RIGIDAMENTE as leis abaixo, adaptando para a linguagem do arquivo atual (Python, Rust, TypeScript/Node, C++) e framework de UI (Svelte, Vue, Electron, Tauri).

---

## 1. ARQUITETURA EM 3 GAVETAS (Desacoplamento Total de UI)
O frontend é descartável e intercambiável; o motor de automação é sagrado. É PROIBIDO misturar lógica de processo com código de tela.

### Gaveta 1: Apresentação (Svelte / Vue / Vite / Tailwind / Renderer Process)
- **Responsabilidade Única:** Exibir estado, renderizar previews, capturar cliques/inputs e enviar intenções via IPC/API local.
- **PROIBIÇÃO ABSOLUTA:** Componentes `.svelte`, `.vue` ou scripts de UI NUNCA devem importar `child_process`, `fs`, `path`, chamar binários (FFmpeg), montar prompts de LLM ou conter loops de retentativa de automação.
- **Portabilidade:** Se o usuário decidir apagar a pasta do Svelte hoje e colocar Vue amanhã, 0% da lógica de automação deve ser perdida. Toda comunicação com o motor deve passar por uma camada de contrato limpa (IPC tipado, WebSockets locais ou HTTP local).

### Gaveta 2: Orquestrador / Core (Node Main Process / Rust Core / Python Service)
- **Responsabilidade Única:** Gerenciar a máquina de estados do pipeline (ex: `IDLE` -> `ROTEIRO` -> `AUDIO` -> `VIDEO` -> `RENDER` -> `DONE`), controlar filas de execução, verificar cache de idempotência e emitir eventos de progresso para a Gaveta 1.
- Não executa operações brutas de I/O diretamente; ele comanda os Executores da Gaveta 3.

### Gaveta 3: Executores e Drivers de I/O (Workers em Python / Rust / Node / C++)
- **Responsabilidade Única:** Funções puras de infraestrutura. Chamar FFmpeg, rodar modelos locais (Whisper/Ollama/TTS), bater em APIs externas, ler/escrever no banco (SQLite) ou disco.
- Devem receber parâmetros fortemente tipados (Structs no Rust, Pydantic/Dataclasses no Python, Interfaces Zod/TS no Node) e retornar resultados padronizados ou erros explícitos.

---

## 2. IDEMPOTÊNCIA E ESCRITA ATÔMICA (Zero Retrabalho / Zero Corrupção)
Processamento de mídia e chamadas de IA custam tempo, hardware e dinheiro. Executar o mesmo job duas vezes DEVE retornar o resultado já pronto instantaneamente.

1. **Hash Determinístico de Etapa:**
   - Antes de executar qualquer etapa pesada, gere um hash (`SHA-256` ou `BLAKE3`) combinando: `input_data + parametros_de_configuracao + versao_do_step`.
   - Exemplo: `step_hash = sha256(texto_do_roteiro + voz_id + velocidade_tts)`.
2. **Checkpoint no Manifesto (SQLite ou JSON de Estado):**
   - Consulte se `step_hash` já consta como `COMPLETED` e se o arquivo de saída existe no disco com tamanho maior que 0 bytes.
   - Se positivo, pule a execução e retorne o caminho do arquivo existente (a menos que a flag `force_rebuild: true` seja explicitamente passada).
3. **Escrita Atômica em Disco (Atomic Write Pattern):**
   - NUNCA grave a saída de um processo (especialmente FFmpeg ou downloads) diretamente no nome final do arquivo (`video_final.mp4`). Se o app fechar ou der crash no meio, no próximo boot o sistema achará que o arquivo corrompido de 2MB está pronto.
   - **Regra:** Grave sempre em `video_final.mp4.tmp.<uuid>`. Somente após o processo retornar `exit_code == 0` e o arquivo ser validado, execute um `rename/move` atômico para `video_final.mp4`.

---

## 3. CONTROLE DE CONCORRÊNCIA E PROTEÇÃO DE HARDWARE (Race Conditions)
Máquinas locais têm limites físicos rígidos de CPU, I/O de disco e especialmente VRAM (GPU).

1. **Semáforo de GPU / Processamento Pesado:**
   - É PROIBIDO disparar múltiplos jobs pesados de GPU/CPU simultaneamente via `Promise.all`, `asyncio.gather` ou threads soltas sem um limitador.
   - Implemente uma fila com concorrência controlada (ex: `Concurrency = 1` para tarefas de VRAM/FFmpeg pesado, `Concurrency = 3` para chamadas de rede/API).
   - *Node/TS:* Use `p-limit` ou fila em memória.
   - *Python:* Use `asyncio.Semaphore(1)` ou `ThreadPoolExecutor(max_workers=1)`.
   - *Rust:* Use `tokio::sync::Semaphore` ou `Mutex`.
2. **Isolamento de Workspace Temporário:**
   - Duas automações jamais podem usar a mesma pasta temporária genérica (`/tmp/render/`).
   - Crie diretórios isolados por execução: `/tmp/app_work/<job_id>_<timestamp>/`. Limpe os arquivos `.tmp` após o sucesso ou mantenha apenas para debug em caso de falha controlada.
3. **Banco de Dados Local (SQLite):**
   - Se usar SQLite para guardar estados de projetos, SEMPRE ative o modo WAL (`PRAGMA journal_mode=WAL;`) e configure `busy_timeout` na inicialização para evitar erros de `SQLITE_BUSY` / `database is locked` quando a UI ler dados enquanto o worker grava progresso.

---

## 4. SEGURANÇA LOCAL, IPC E EXECUÇÃO DE SUBPROCESSOS
Mesmo sem login na nuvem, aplicativos locais sofrem com injeção de comando (quando títulos de roteiros ou nomes de arquivos têm caracteres especiais) e vazamento de chaves de API.

1. **Proibição de Concatenação de Strings em Shell:**
   - NUNCA chame subprocessos concatenando strings com `shell=True` (Python) ou `exec()` (Node). Um simples título de roteiro com aspas ou `;` quebra o FFmpeg ou executa código malicioso.
   - **Correto (Array de Argumentos):**
     - *Python:* `subprocess.run(["ffmpeg", "-i", input_path, output_path], shell=False, check=True)`
     - *Node/TS:* `spawn("ffmpeg", ["-i", inputPath, outputPath], { shell: false })`
     - *Rust:* `Command::new("ffmpeg").arg("-i").arg(input_path).arg(output_path)`
2. **Gestão de Processos Zumbis (Orphan Processes):**
   - Em aplicativos Desktop (Electron/Tauri/Python GUI), se o usuário cancelar a tarefa ou fechar a janela, os subprocessos filhos (FFmpeg, servidores Python/Ollama, containers) NÃO podem continuar rodando escondidos no sistema operacional consumindo 100% de CPU/GPU.
   - Sempre amarre o ciclo de vida do subprocesso a um `AbortController` (Node/TS), `kill_on_drop(true)` (Rust/Tokio) ou gerenciador de grupo de processos (`SIGTERM`/`SIGKILL` no `atexit` do Python).
3. **Blindagem de Chaves e Variáveis de Ambiente:**
   - Prefixos como `VITE_`, `PUBLIC_` ou `NEXT_PUBLIC_` expõem o valor diretamente no pacote do frontend. NUNCA coloque chaves de API de LLM, áudio ou serviços pagos com esses prefixos.
   - No Electron, mantenha `contextIsolation: true` e `nodeIntegration: false`. Exponha apenas canais específicos via `contextBridge` no script de `preload`.

---

## 5. DOCKER E AMBIENTES DE EXECUÇÃO (Quando Aplicável)
Se a automação envolver containers Docker:
- Mapeie volumes de entrada/saída com caminhos absolutos validados.
- Nunca salve o estado do pipeline apenas dentro do container efêmero; persista o manifesto de idempotência e os arquivos finais no volume do host.
- Defina limites claros de memória e restart policies para evitar travamento do host.

---

## 6. Verificação de Entrega & Custódia (Protocolo Henke)

Esta skill opera sob a autoridade estrita do [PROTOCOLO.md](../PROTOCOLO.md) da Suíte Henke. Antes de declarar qualquer tarefa como concluída, confirme mentalmente cada item:

- [ ] **Entrega Real (Zero Stubs)**: Funcionalidade entregue de verdade até a ponta, sem cascas vazias, mocks permanentes ou comentários delegando a solução para depois.
- [ ] **Regra Portão vs Produto**: Se o teste conflitou com a nova exigência, o teste foi corrigido — sem hacks como `.sr-only`, textos invisíveis ou dados falsos.
- [ ] **Ledger de Evidência**: Comandos executáveis documentados no relatório para que o auditor possa re-executar os testes verbatim.
- [ ] **Custódia de Ambiente**: Branch e worktree corretas, `git status` limpo e variáveis de ambiente apontando para os binários compilados na worktree ativa.
- [ ] **Ressalvas Transparentes**: Seção "O que NÃO foi verificado" preenchida com clareza (testes não rodados por falta de hardware, APIs externas não chamadas, etc.).
