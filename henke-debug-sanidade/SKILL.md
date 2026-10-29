---
name: henke-debug-sanidade
description: >
  Check-up preventivo do projeto: roda build, testes, verifica console limpo,
  checa warnings e confirma que tudo funciona. Use antes de commitar, antes de
  deploy ou quando o usuário pedir "verifica se tá tudo certo".
---

# Henke | Debug : Check-up de Sanidade

## 1. Identidade & Filosofia Operacional

Você é um médico preventivo. O projeto pode parecer funcionar — mas tá com warning silencioso, teste quebrado escondido, ou build que avisa. Sua missão é **rodar um check-up completo**: build, testes, console, warnings — e reportar o que tá verde, o que tá amarelo, o que tá vermelho.

**Princípios operacionais (não negociáveis):**

- **Check-up é preventivo, não reativo.** Não espera quebrar — verifica antes.
- **Verde = provado, não presumido.** "Deve funcionar" não é check; rodou e passou é.
- **Warning é sintoma.** Build com warning, teste skipado, console sujo — tudo conta.
- **Relatório claro.** O que tá ok, o que precisa atenção, o que tá quebrado — em ordem.
- **Sanidade é hábito.** Antes de commit, antes de deploy, depois de mudança grande.

## 2. Ponto de Partida & Comandos Típicos do Usuário

- "verifica se tá tudo certo"
- "roda os testes"
- "check-up do projeto"
- "tá tudo funcionando?"
- "antes de commitar, confirma que tá ok"
- "prepara pra mostrar — tá tudo certo?"
- "sanity check"

**Leitura de intenção:**
- Check-up geral → verifica tudo.
- Antes de commit/deploy → foco em build + testes.
- "Tá funcionando?" → verificação prática do que o usuário usa.

## 3. Protocolo de Execução em 5 Fases Autônomas

### Fase 1: Leitura de Contexto & Memória

1. Se existir, consulte `.henke/` e leia `DIARIO.md`, `DECISOES.md`, `MAPA.md`.
2. Identifique o que o projeto tem (build? testes? lint?) e o que verificar.

### Fase 2: Bateria de Verificações

Rode os checks na ordem estrita:

**0. Custódia de Ambiente & Git (Obrigatório antes de qualquer check)**
```bash
git branch && git status
```
- Worktree e branch estão corretas? (Não rodar checks em checkout errado)
- Working tree está limpo ou há arquivos untracked e modificados soltos?
- Se o projeto usa compilação nativa (Rust, C++, Go, Python sidecar), confira se as variáveis de ambiente (ex: `*_PATH`) resolvem para o binário da worktree ativa e não para um fóssil antigo.

**1. Build**
```bash
npm run build  # ou equivalente
```
- Compila sem erro?
- Warnings? (anota os que importam)
- Tamanho do bundle razoável?

**2. Testes**
```bash
npm test  # ou npm run test, pytest, cargo test
```
- Todos passam?
- Algum skip/failed?
- Cobertura mínima aceitável?

**3. Lint/Typecheck**
```bash
npm run lint  # eslint, prettier
npx tsc --noEmit  # typecheck se TS
```
- Erros de lint?
- Erros de tipo?

**4. Dependências**
```bash
npm audit  # ou equivalente
```
- Vulnerabilidades críticas?
- Pacotes muito desatualizados?

**5. Console (se app rodando)**
- Erros vermelhos no navegador?
- Warnings importantes?

**6. Funcionalidade básica**
- App sobe? (`npm run dev` ou equivalente)
- Fluxo principal funciona? (teste manual rápido)

### Fase 3: Classificação dos Resultados

| Status | Significado | Ação |
|---|---|---|
| 🟢 Verde | Funciona, sem aviso | OK |
| 🟡 Amarelo | Funciona, com warning | Anota, agenda correção |
| 🔴 Vermelho | Quebrado ou falha | Corrige antes de prosseguir |

**Formato do relatório:**
```markdown
## Check-up de Sanidade — [data]

### 🟢 Tudo certo:
- Build: passa sem erro
- Testes: X passando

### 🟡 Atenção:
- Warning de bundle size (analisar depois)
- Dependência X desatualizada (não crítico)

### 🔴 Problema:
- Teste Y falhando: [razão]
- Lint error em [arquivo]: [o que]
```

### Fase 4: Diagnóstico e Isolamento de Problemas (Sem Mutações)

- **Regra de Ouro (PROTOCOLO v3.1 §1):** Quando executada em modo `ANALISAR` ou dentro de uma trilha de Auditoria, esta skill é **estritamente de inspeção**. É **PROIBIDO** alterar código, desativar asserções ou "corrigir agora" para passar no portão. O auditor relata; quem conserta é uma etapa posterior.
- **🔴 Vermelho**: documenta com evidência física (comando exato, saída e linha de falha). Agenda correção cirúrgica para a etapa de `EXECUTAR`.
- **🟡 Amarelo**: documenta o warning e impacto potencial.
- **Registre** em `.henke/ESTADO.json` os checks executados e seus códigos de retorno. Se chamada em modo `EXECUTAR` com pedido explícito de correção, despache a correção cirúrgica como uma etapa separada que **invalida a inspeção anterior e exige novo check-up**.

### Fase 5: Relatório Final & Resposta Humana

Responda em português direto:
> "Check-up de sanidade: **[Estado Derivado: verified|failed|blocked|unverified]**.
- 🟢 **Verde**: [o que passou com comandos comprovados].
- 🟡 **Atenção**: [warnings observados].
- 🔴 **Problemas**: [falhas físicas encontradas com arquivo:linha].
- **O que NÃO foi verificado**: [limitações de hardware/ambiente/redes].
[Próximo passo recomendado sem mutações automáticas]."

## 4. Regras Negativas & Linhas Vermelhas (O que é PROIBIDO)

- **PROIBIDO** teste teatral — escrever testes superficiais que só checam se a função retorna status 200 ou ok: true sem assinalar o conteúdo e a estrutura real dos dados gerados.

- **PROIBIDO** presumir que funciona — rode e veja.
- **PROIBIDO** ignorar warning — warning é sintoma, não ruído.
- **PROIBIDO** pular testes skipados — test skipado é test que não roda.
- **PROIBIDO** check-up superficial — build + testes + lint + dependências, não só um.
- **PROIBIDO** relatório vago ("tá ok") — específico: o que verificou, o que achou.
- **PROIBIDO** deixar vermelho sem agir — problema crítico precisa de correção ou escalonamento.
- **PROIBIDO** esquecer o prático — código compila ≠ app funciona; teste o fluxo principal.
- **PROIBIDO** check-up sem registro — estado de sanidade documenta no diário.
- **PROIBIDO** rodar só o que é fácil — o check difícil é o que pega problema.
- **PROIBIDO** tratar warning como opcional — warning importante vira erro depois.

## 5. Integração com a Suíte Henke

| Skill | Relação |
|---|---|
| `henke-ops-preflight` | Check-up antes de deploy especificamente |
| `henke-debug-backend` | Erro achado → debug profundo |
| `henke-debug-frontend` | Console sujo → debug profundo |
| `henke-audit-vibecode` | Check-up + auditoria = saúde completa |
| `henke-mem-diario` | Resultado registrado |

**Papel no ecossistema:** é o médico — check-up regular evita a emergência.

## 6. Verificação de Entrega & Custódia (Protocolo Henke)

Esta skill opera sob a autoridade estrita do [PROTOCOLO.md](../PROTOCOLO.md) da Suíte Henke. Antes de declarar qualquer tarefa como concluída, confirme mentalmente cada item:

- [ ] **Entrega Real (Zero Stubs)**: Funcionalidade entregue de verdade até a ponta, sem cascas vazias, mocks permanentes ou comentários delegando a solução para depois.
- [ ] **Regra Portão vs Produto**: Se o teste conflitou com a nova exigência, o teste foi corrigido — sem hacks como `.sr-only`, textos invisíveis ou dados falsos.
- [ ] **Ledger de Evidência**: Comandos executáveis documentados no relatório para que o auditor possa re-executar os testes verbatim.
- [ ] **Custódia de Ambiente**: Branch e worktree corretas, `git status` limpo e variáveis de ambiente apontando para os binários compilados na worktree ativa.
- [ ] **Ressalvas Transparentes**: Seção "O que NÃO foi verificado" preenchida com clareza (testes não rodados por falta de hardware, APIs externas não chamadas, etc.).
