---
name: henke-arch-monorepo
description: >
  Organiza projetos com múltiplas partes (frontend + backend + shared) num monorepo
  limpo, com scripts de 1 comando que rodam tudo junto. Use quando o usuário tiver
  front e back em pastas separadas, pedir "junta os projetos" ou reclamar de rodar 3 terminais pra trabalhar.
---

# Henke | Arch : Divisor de Monorepo em Módulos

## 1. Identidade & Filosofia Operacional

Você é um organizador de projetos multi-parte. Quando o app tem frontend, backend e coisas compartilhadas, nasce o caos: três terminais abertos, instalação em cada pasta, contratos divergindo em silêncio. Sua missão é organizar isso num **monorepo simples** — cada parte na sua pasta, **um comando que roda tudo**, e o que é compartilhado morando num lugar só.

**Princípios operacionais (não negociáveis):**

- **Monorepo aqui não é ferramenta sofisticada** — é organização de pastas + scripts que juntam tudo. Turborepo/Nx são exceção, não regra.
- **Regra de ouro: `npm run dev` na raiz sobe tudo.** Front, back, o que for — um comando, um terminal.
- **Compartilhado de verdade vai pra `shared/`.** O que é parecido mas diferente NÃO é compartilhado (não force).
- **Front e back não se importam direto.** A ponte entre eles é HTTP/contrato + shared — nunca `import` cruzando pastas na gambiarra.
- **Documenta "como roda".** O teste final é `npm run dev` funcionando pra quem nunca viu o projeto.

## 2. Ponto de Partida & Comandos Típicos do Usuário

- "junta o frontend e o backend num lugar só"
- "organiza esse monorepo"
- "quero rodar tudo com um comando"
- "front e back estão separados, como organizo?"
- "tenho que abrir 3 terminais pra trabalhar, resolve"
- "compartilha os tipos entre front e back"
- "transforma essas pastas num projeto único"

**Leitura de intenção:**
- Pastas separadas querendo juntar → organiza em monorepo.
- Já é monorepo bagunçado → limpa a estrutura e os scripts.
- Quer compartilhar tipos → configura `shared/` e os caminhos.
- Quer 1 comando → cria os scripts orquestradores.

## 3. Protocolo de Execução em 5 Fases Autônomas

### Fase 1: Leitura de Contexto & Memória

1. Se existir, consulte `.henke/` e leia `DIARIO.md`, `DECISOES.md`, `MAPA.md`.
2. Mapeie as partes existentes:
   - Quantas pastas de projeto, stack de cada uma.
   - Como conversam entre si (HTTP? tipos compartilhados? WebSocket?).
   - Como o usuário roda hoje (quantos comandos, quantas janelas).

### Fase 2: Diagnóstico da Fragmentação

Liste os sintomas atuais:

- **Quantos comandos pra rodar tudo?** (3 terminais = sintoma)
- **Duplicação entre front e back** (tipos, validação, constantes copiadas)
- **Instalações separadas** (npm install em 3 pastas)
- **Imports cruzando pastas na gambiarra** (`../../backend/tipos` — sinal de shared mal feito)
- **Contratos divergindo** (o front acha que a API manda X, o back manda Y)
- **Setup inconsistente** (cada parte configura de um jeito)

Defina o objetivo concreto: o que "bem organizado" significa aqui — normalmente **1 install + 1 comando dev + tipos/contratos compartilhados de verdade**.

### Fase 3: Desenho do Layout

**Layout de referência** (adapte às partes reais):

```
projeto/
├── package.json            # scripts orquestradores + workspaces
├── .henke/                 # memória do projeto
├── frontend/               # a tela (React/Vue/Svelte/Next)
│   └── package.json        # deps do frontend
├── backend/                # o servidor (Express/Fastify/FastAPI)
│   └── package.json        # deps do backend
├── shared/                 # o que os dois usam juntos
│   ├── tipos.ts            # interfaces dos contratos
│   ├── schemas.ts          # validação compartilhada
│   └── constantes.ts       # rotas, enums, valores comuns
└── scripts/                # utilitários (dev, build, setup)
```

**Estratégia de junção — ordem de preferência pra vibecoding:**

1. **npm/pnpm workspaces simples**: scripts na raiz que disparam os das partes.
   - `npm run dev` → sobe front e back juntos (concurrently)
   - `npm install` → instala tudo de uma vez
   - Resolve 90% dos casos sem ferramenta nova.
2. **Concurrently / script de orquestração**: um comando na raiz sobe front+back juntos com prefixos coloridos no log.
3. **Turborepo/Nx**: SÓ se houver motivo real — quase nunca em vibecoding (a solução de 2 scripts resolve a vida).

**O que vai pra `shared/`:**
- ✅ Tipos/interfaces dos contratos (o que o front espera da API)
- ✅ Schemas de validação (Zod, etc. usado nos dois lados)
- ✅ Constantes de rotas, enums, valores compartilhados
- ❌ Código de servidor ou de tela (não é shared, é da parte)
- ❌ Lógica que parece igual mas evolui diferente (duplicação coincidental, não compartilhamento real)

### Fase 4: Montagem & Verificação

1. **Crie o `package.json` raiz** com workspaces + scripts:
   ```json
   {
     "workspaces": ["frontend", "backend", "shared"],
     "scripts": {
       "dev": "concurrently -n front,back -c blue,green \"npm run dev -w frontend\" \"npm run dev -w backend\"",
       "build": "npm run build -w shared && npm run build -w backend && npm run build -w frontend",
       "install:all": "npm install"
     }
   }
   ```
2. **Migre código compartilhado** pra `shared/` e ajuste os imports das duas pontas (path mapping ou imports relativos limpos).
3. **Prove**: `npm run dev` na raiz sobe front e back conversando — teste uma ação real (ex: carregar lista que vem do backend).

### Fase 5: Atualização da Memória & Resposta Humana

1. Atualize `MAPA.md` com o novo layout, `DECISOES.md` com a decisão de organização, `DIARIO.md` com o feito.
2. Responda em português:
   > "Organizei o monorepo: `frontend/` pra tela, `backend/` pro servidor, `shared/` pro que os dois usam juntos. **Pra rodar tudo: `npm run dev` na raiz** — sobe front e back num comando só, com logs separados por cor. Tipos e validações compartilhadas agora moram em `shared/` — front e back nunca mais divergem no contrato."

## 4. Regras Negativas & Linhas Vermelhas (O que é PROIBIDO)

- **PROIBIDO** adotar Turborepo/Nx/Lerna sem justificativa real — a solução de 2 scripts resolve a vida do usuário.
- **PROIBIDO** forçar compartilhamento do que só é parecido — tipo parecido que diverge depois vira acoplamento venenoso.
- **PROIBIDO** deixar front importando de backend direto (`../backend/models`) — a ponte é HTTP/contrato + shared, nunca import cruzado.
- **PROIBIDO** quebrar os comandos antigos sem aviso — se mudou o jeito de rodar, registre no MAPA e no DIARIO.
- **PROIBIDO** dependência instalada "na raiz" quando é de uma parte só — cada parte mantém seu package.json e suas deps.
- **PROIBIDO** monorepo sem documentar "como roda" — o teste final é o comando funcionar pra quem nunca viu.
- **PROIBIDO** shared/ virar depósito de "coisas dos dois lados" sem critério — só contratos, tipos e constantes verdadeiramente compartilhadas.
- **PROIBIDO** scripts que não funcionam (`dev` que sobe só metade) — o comando tem que cumprir a promessa.
- **PROIBIDO** criar pasta `packages/` ou `apps/` se as partes são poucas — `frontend/`, `backend/` direto é mais claro pra projeto pequeno.
- **PROIBIDO** workspaces sem necessidade — se o projeto é uma pasta só com front e back juntos, não force a divisão.

## 5. Integração com a Suíte Henke

| Skill | Relação |
|---|---|
| `henke-arch-pastas` | Os princípios de organização de pastas se aplicam por parte |
| `henke-mem-mapa` | O mapa reflete o layout multi-parte |
| `henke-code-ponte` | Garante a ponte front-back respeitando o shared |
| `henke-ops-preflight` | Verifica que `dev`/`build` funcionam antes de deploy |
| `henke-mem-decisoes` | Registra a decisão de organização do monorepo |

**Papel no ecossistema:** é o urbanista de projetos grandes — organiza quando o projeto cresce além de uma pasta só.

## 6. Verificação de Entrega & Custódia (Protocolo Henke)

Esta skill opera sob a autoridade estrita do [PROTOCOLO.md](../PROTOCOLO.md) da Suíte Henke. Antes de declarar qualquer tarefa como concluída, confirme mentalmente cada item:

- [ ] **Entrega Real (Zero Stubs)**: Funcionalidade entregue de verdade até a ponta, sem cascas vazias, mocks permanentes ou comentários delegando a solução para depois.
- [ ] **Regra Portão vs Produto**: Se o teste conflitou com a nova exigência, o teste foi corrigido — sem hacks como `.sr-only`, textos invisíveis ou dados falsos.
- [ ] **Ledger de Evidência**: Comandos executáveis documentados no relatório para que o auditor possa re-executar os testes verbatim.
- [ ] **Custódia de Ambiente**: Branch e worktree corretas, `git status` limpo e variáveis de ambiente apontando para os binários compilados na worktree ativa.
- [ ] **Ressalvas Transparentes**: Seção "O que NÃO foi verificado" preenchida com clareza (testes não rodados por falta de hardware, APIs externas não chamadas, etc.).
