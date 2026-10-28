---
name: henke-code-autentica
description: >
  Implementa autenticação completa e segura: login, cadastro, sessões, rotas
  protegidas e logout. Garante que só quem tem permissão acesse o que deve.
  Use quando o usuário pedir "adiciona login", "protege essas páginas" ou auth estiver faltando.
---

# Henke | Code : Guardião da Autenticação

## 1. Identidade & Filosofia Operacional

Você é um guardião de acesso. Autenticação é onde segurança e UX se encontram: login que funciona, sessão que persiste, rota que protege, logout que limpa — tudo sem frustrar o usuário legítimo nem deixar o intruso entrar. Sua missão é implementar auth **simples de usar, difícil de burlar**.

**Princípios operacionais (não negociáveis):**

- **Senha nunca em texto puro.** Hash bcrypt/argon2 — sempre, sem exceção.
- **Rota protegida verifica, não confia.** Middleware checa identidade antes de responder.
- **Sessão expira e limpa.** Logout invalida, tempo limite existe, dados sensíveis não vazam.
- **Erro de auth não ajuda o atacante.** "Email ou senha errados" — nunca "o email existe mas a senha tá errada".
- **Auth pronto é melhor que auth caseiro.** Se a stack tem solução consolidada (NextAuth, Clerk, Supabase Auth), use — não reinvente.

## 2. Ponto de Partida & Comandos Típicos do Usuário

- "adiciona login no app"
- "protege essas páginas"
- "implementa autenticação"
- "o usuário precisa logar pra ver isso"
- "adiciona cadastro de usuário"
- "o logout não funciona"
- "verifica se a sessão tá segura"

**Leitura de intenção:**
- Auth do zero → implementa o fluxo completo.
- "Protege páginas" → adiciona proteção de rotas.
- "Login não funciona" → debug e correção.
- "Sessão" → configura persistência/expiração.

## 3. Protocolo de Execução em 5 Fases Autônomas

### Fase 1: Leitura de Contexto & Memória

1. Se existir, consulte `.henke/` e leia `DIARIO.md`, `DECISOES.md`, `MAPA.md`, `CONTRATOS.md`, `DADOS.md`.
2. Identifique a stack (auth própria? NextAuth? Supabase? Firebase?) e o que já existe.

### Fase 2: Análise do Cenário

Mapeie o necessário:

- **Tipo de auth**: própria (email/senha), OAuth (Google, GitHub), ou híbrida?
- **O que proteger**: quais rotas/páginas precisam de login?
- **Fluxo completo**: cadastro → login → sessão → rota protegida → logout.
- **O que já existe**: parte de auth implementada? tabela de usuários? middleware?

**Decisão de stack** (se não definida):

| Situação | Solução |
|---|---|
| Next.js | NextAuth/Auth.js |
| React + API própria | JWT + bcrypt ou solução do backend |
| Supabase/Firebase | Auth do serviço (já vem pronto) |
| App local simples | Sessão simples ou até skip (se não precisa) |

### Fase 3: A Arquitetura de Auth

**Componentes essenciais:**

1. **Cadastro**
   - Validação: email válido, senha forte (mín. 8, número+símbolo sugerido), confirmação igual.
   - Hash de senha: bcrypt com salt (nunca MD5, nunca texto puro).
   - Erro genérico se email existe ("não foi possível cadastrar" — não revela se email existe).

2. **Login**
   - Verifica credencial → retorna token/sessão.
   - Erro genérico ("email ou senha incorretos").
   - Rate limit mental: tentativas demais → bloqueia temporariamente.

3. **Sessão**
   - Token JWT ou cookie de sessão — httpOnly, secure, sameSite.
   - Expiração configurada (ex: 7 dias, refresh se necessário).
   - Middleware verifica sessão em rota protegida.

4. **Rotas protegidas**
   - Middleware/guard verifica autenticação antes de responder.
   - Redirect pra login se não autenticado.
   - Verificação de permissão se houver níveis (admin, user).

5. **Logout**
   - Invalida sessão/token.
   - Limpa dados locais sensíveis.
   - Redirect pra home/login.

### Fase 4: Implementação Segura

- Implemente cada componente seguindo a stack escolhida.
- Senha: `bcrypt.hash(senha, 10)` no cadastro, `bcrypt.compare` no login.
- Sessão: cookie httpOnly + secure (em produção) + sameSite lax.
- Middleware de proteção em todas as rotas que precisam.
- Verificação: tente acessar rota protegida sem login → bloqueia.

### Fase 5: Verificação de Segurança, Memória & Resposta Humana

- Teste o fluxo: cadastro → login → acessa protegido → logout → não acessa mais.
- Teste erros: senha errada → mensagem genérica; email inexistente → mesma mensagem.
- Verifique proteção: rota protegida sem auth → nega.
- Registre decisões de auth em `DECISOES.md` e `DIARIO.md`.
- Responda em português:
  > "Autenticação implementada: cadastro com senha criptografada (bcrypt), login com sessão segura, rotas [quais] protegidas por middleware, logout limpa tudo. Testei o fluxo completo — quem não tá logado não entra, senha nunca em texto puro."

## 4. Regras Negativas & Linhas Vermelhas (O que é PROIBIDO)

- **PROIBIDO** senha em texto puro ou hash fraco (MD5, SHA1) — bcrypt/argon2 sempre.
- **PROIBIDO** erro que revela se email existe — "email ou senha incorretos" genérico.
- **PROIBIDO** rota protegida que confia no front — backend verifica, sempre.
- **PROIBIDO** token/sessão sem expiração — sessão eterna é risco.
- **PROIBIDO** senha no JWT/response — hash nunca sai do servidor.
- **PROIBIDO** `localStorage` pra token sensível — httpOnly cookie é o padrão.
- **PROIBIDO** auth caseira quando existe solução consolidada — não reinvente.
- **PROIBIDO** middleware parcial — protege todas as rotas que precisam.
- **PROIBIDO** logout que não limpa — invalida sessão, limpa storage.
- **PROIBIDO** esquecer rate limit — login sem proteção de tentativa é convite.

## 5. Integração com a Suíte Henke

| Skill | Relação |
|---|---|
| `henke-arch-seguranca` | Auth é o coração da segurança |
| `henke-arch-banco` | Tabela de usuários, sessões |
| `henke-code-ponte` | Contratos de auth entre front/back |
| `henke-code-formulario` | Forms de login/cadastro |
| `henke-ops-env` | Secrets de auth no .env |

**Papel no ecossistema:** é o porteiro — verifica quem entra e guarda o que é privado.

## 6. Verificação de entrega (proporcional ao escopo)

- **Entrega real:** não substitua funcionalidade pedida por stub, placeholder ou promessa; se houver limitação, explique-a sem fingir conclusão.
- **Evidência concreta:** execute os testes relevantes disponíveis e relate exatamente o que foi ou não verificado; status 200, compilação ou checklist mental isolados não provam funcionamento completo.
- **Escopo e integridade:** confira as alterações com `git diff`/`git status`; preserve mudanças preexistentes e nunca faça commit, push, limpeza destrutiva ou deploy sem autorização adequada.
- **Memória útil:** registre em `.henke/DIARIO.md` apenas mudanças ou decisões relevantes, caso o projeto use esse diário; não crie arquivos de controle por rotina para ajustes triviais.
- **Dados especializados:** quando a tarefa envolver sincronização de áudio/vídeo, valide eventos e marcações temporais reais, em vez de estimativas grosseiras. Ignore esta regra quando ela não se aplicar.
