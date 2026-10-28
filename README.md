# Suíte Henke — 47 Macro-Skills para Vibecoding Autônomo

> **O arsenal completo para desenvolvimento autônomo e vibecoding de ponta a ponta, sem jargões corporativos, com execução profunda e blindagem contra código frágil.**

Compatível nativamente com **Antigravity**, **Claude Code**, **Codex**, **Cursor** e qualquer ambiente que suporte o padrão aberto **Agent Skills (`SKILL.md`)**.

---

## 🎯 Por que a Suíte Henke existe?

O ecossistema de desenvolvimento assistido por IA frequentemente falha em dois extremos:
1. **Micro-skills atômicas:** Forçam você a memorizar 30 comandos técnicos pequenos e amarrá-los na mão.
2. **Prompts genéricos:** Fazem a IA adivinhar soluções, reescrever arquivos inteiros, inventar gambiarras frágeis e esquecer o que fez na sessão anterior.

A **Suíte Henke** adota o conceito de **Macro-Skills de Longo Alcance**:
* **Comandos curtos e diretos:** Você só precisa dizer coisas simples como `"debuga o chat do meu aplicativo"`, `"organiza as pastas desse app"` ou `"tira essa cara feia de IA da tela"`.
* **Execução autônoma em 5 fases:** A skill assume uma postura sênior nos bastidores — lê a memória local, investiga a causa raiz sem chutar, aplica a menor alteração cirúrgica necessária (Minimal Diff), roda os testes reais do sistema e atualiza silenciosamente o diário de bordo do projeto.
* **Linguagem humana vale mais que ouro:** Respostas sempre em português claro, direto e focado no resultado prático.
* **Autossuficiência total:** 47 skills cobrindo da concepção no guardanapo até a publicação na nuvem, sem depender de nenhuma skill externa.

---

## 🧭 As 47 Macro-Skills por Categoria

### 1. Concepção & Ideias (`henke-plan-*`)
* [`henke-plan-radar`](./henke-plan-radar/SKILL.md): Radar de referências e scraping na web para alimentar o app com soluções reais.
* [`henke-plan-ideias`](./henke-plan-ideias/SKILL.md): Construtor de produto — transforma rascunhos soltos em conceitos viáveis.
* [`henke-plan-destrava`](./henke-plan-destrava/SKILL.md): Destravador criativo — sugere as 3 próximas features mais lógicas e divertidas.
* [`henke-plan-mvp`](./henke-plan-mvp/SKILL.md): Guilhotina de escopo — corta excessos para rodar a primeira versão hoje.
* [`henke-plan-historias`](./henke-plan-historias/SKILL.md): Tradutor de desejos em cartões de tarefas sequenciais para a IA.
* [`henke-plan-fluxo`](./henke-plan-fluxo/SKILL.md): Mapeador de telas e jornadas completas do usuário com diagramas.

### 2. Memória Contínua & Diário (`henke-mem-*`)
* [`henke-mem-diario`](./henke-mem-diario/SKILL.md): Diário de bordo automático em `.henke/DIARIO.md` para a IA nunca esquecer nada.
* [`henke-mem-retomada`](./henke-mem-retomada/SKILL.md): Recuperador de contexto — "Onde parei?" explicado em 1 minuto.
* [`henke-mem-decisoes`](./henke-mem-decisoes/SKILL.md): Cofre de decisões em `.henke/DECISOES.md` blindando o app contra regressões.
* [`henke-mem-mapa`](./henke-mem-mapa/SKILL.md): Mapa do tesouro da codebase explicando o papel de cada pasta e arquivo.
* [`henke-mem-faxina`](./henke-mem-faxina/SKILL.md): Caçador de arquivos zumbis, temporários e código abandonado.

### 3. Arquitetura & Estrutura (`henke-arch-*`)
* [`henke-arch-stack`](./henke-arch-stack/SKILL.md): Seleção enxuta de tecnologias leves (bloqueia frameworks pesados).
* [`henke-arch-pastas`](./henke-arch-pastas/SKILL.md): Estrutura de 3 Gavetas (UI, Core/Orquestrador e Executores I/O).
* [`henke-arch-desmembrar`](./henke-arch-desmembrar/SKILL.md): Desmembra arquivos monstros de 2.000 linhas em módulos profundos sem quebrar nada.
* [`henke-arch-monorepo`](./henke-arch-monorepo/SKILL.md): Orquestrador de monorepos híbridos (Electron + Front + Python/Rust) com 1 comando.
* [`henke-arch-banco`](./henke-arch-banco/SKILL.md): Modelagem simples de SQLite (modo WAL, busy_timeout) e Postgres.
* [`henke-arch-seguranca`](./henke-arch-seguranca/SKILL.md): Blindagem contra comandos vulneráveis e isolamento de processos.
* [`henke-blindagem`](./henke-blindagem/SKILL.md): Diretrizes universais de blindagem, escrita atômica e concorrência segura.

### 4. Visual Profissional & Anti-AI-Slop (`henke-ui-*`)
* [`henke-ui-antislop`](./henke-ui-antislop/SKILL.md): Exterminador de clichês de IA (gradientes roxo/neon, cards aninhados, emojis em botões).
* [`henke-ui-paleta`](./henke-ui-paleta/SKILL.md): Gerador de paletas harmônicas com tokens semânticos e contraste real.
* [`henke-ui-tipografia`](./henke-ui-tipografia/SKILL.md): Mestre da hierarquia de fontes, entrelinhas e espaçamentos padronizados.
* [`henke-ui-estados`](./henke-ui-estados/SKILL.md): Construtor dos 4 estados vitais (Vazio, Carregando, Erro Amigável, Sucesso).
* [`henke-ui-feedback`](./henke-ui-feedback/SKILL.md): Resposta imediata a cliques, spinners táteis e trava contra clique duplo.
* [`henke-ui-responsivo`](./henke-ui-responsivo/SKILL.md): Ajuste fluido para celular, tablet, TV e monitores ultrawide sem barras duplas.
* [`henke-ui-auditoria`](./henke-ui-auditoria/SKILL.md): Nota de design de 0 a 100 com plano de melhoria visual cirúrgico.

### 5. Vibecoding Puro & Pontes (`henke-code-*`)
* [`henke-code-ponte`](./henke-code-ponte/SKILL.md): Conexão infalível de botões da interface com rotas e IPC do servidor.
* [`henke-code-mock`](./henke-code-mock/SKILL.md): Fábrica de dados fictícios hiper-realistas para testar telas sem esperar backend.
* [`henke-code-cirurgico`](./henke-code-cirurgico/SKILL.md): Implementação milimétrica (Minimal Diff) — mexe só nas linhas pedidas.
* [`henke-code-formulario`](./henke-code-formulario/SKILL.md): Formulários com validação inteligente e persistência das últimas escolhas.
* [`henke-code-autentica`](./henke-code-autentica/SKILL.md): Login, cadastro e sessão persistente sem transformar o código num labirinto.
* [`henke-code-integracao`](./henke-code-integracao/SKILL.md): Conexão resiliente com APIs externas, webhooks e gateways de pagamento.
* [`henke-code-comentarios`](./henke-code-comentarios/SKILL.md): Tradutor de código em comentários em português direto e claro.
* [`henke-code-3d-scene`](./henke-code-3d-scene/SKILL.md): Criação e otimização de cenas 3D (Three.js/Canvas) com liberação correta de VRAM.

### 6. Debugging Profundo & Caça-Bugs (`henke-debug-*`)
* [`henke-debug-backend`](./henke-debug-backend/SKILL.md): Detetive forense de rotas, banco, workers e processos em segundo plano.
* [`henke-debug-frontend`](./henke-debug-frontend/SKILL.md): Raio-X de telas brancas, botões mudos e estados reativos travados.
* [`henke-debug-raiz`](./henke-debug-raiz/SKILL.md): Investigador científico de causa raiz — proíbe chutes e exige prova do erro.
* [`henke-debug-console`](./henke-debug-console/SKILL.md): Limpador implacável de avisos amarelos e erros vermelhos do terminal.
* [`henke-debug-rede`](./henke-debug-rede/SKILL.md): Especialista em falhas de comunicação, portas ocupadas e bloqueios de CORS.
* [`henke-debug-sanidade`](./henke-debug-sanidade/SKILL.md): Check-up relâmpago de 30 segundos testando se os órgãos vitais continuam de pé.
* [`henke-debug-reversao`](./henke-debug-reversao/SKILL.md): Máquina do tempo — desfaz lambanças de IA com segurança sem apagar seus dados.

### 7. Auditoria de Risco & Saúde (`henke-audit-*` / `henke-refactor-*`)
* [`henke-audit-vibecode`](./henke-audit-vibecode/SKILL.md): Pente-fino caçando gambiarras, condições de corrida e processos zumbis.
* [`henke-audit-seguranca`](./henke-audit-seguranca/SKILL.md): Varredura rigorosa de credenciais vazadas e permissões frouxas.
* [`henke-refactor-duplicado`](./henke-refactor-duplicado/SKILL.md): Unifica lógicas repetidas em uma única fonte da verdade limpa.
* [`henke-refactor-nomes`](./henke-refactor-nomes/SKILL.md): Renomeia variáveis e funções esquisitas para termos claros do mundo real.

### 8. Deploy & Publicação (`henke-ops-*`)
* [`henke-ops-env`](./henke-ops-env/SKILL.md): Gerenciador seguro de arquivos `.env` e geração de `.env.example`.
* [`henke-ops-preflight`](./henke-ops-preflight/SKILL.md): Check-in do avião — roda compilação de produção e testes antes de subir pro ar.
* [`henke-ops-deploy`](./henke-ops-deploy/SKILL.md): Automação para gerar instaladores desktop (.exe) ou publicar na nuvem (VPS/Docker/Vercel).

---

## ⚡ Como Instalar

### Opção 1: Windows (PowerShell)
Clone o repositório e rode o instalador de 1 comando:
```powershell
git clone https://github.com/ErickHenke/henke-skills.git
cd henke-skills
powershell -ExecutionPolicy Bypass -File .\install.ps1
```
> O script cria automaticamente as pastas e os hardlinks em `~/.agents/skills`, `~/.gemini/config/skills` e `~/.claude/skills`.

### Opção 2: Linux / macOS (Bash)
```bash
git clone https://github.com/ErickHenke/henke-skills.git
cd henke-skills
chmod +x install.sh && ./install.sh
```

---

## 📄 Licença

Distribuído sob a licença **MIT**. Veja [`LICENSE`](./LICENSE) para mais detalhes.
