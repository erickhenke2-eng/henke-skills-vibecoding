# Protocolo Henke de Verificação & Custódia (Anti-Migué v2.0)

> **"Regra em prosa é sugestão. O que protege código de verdade é portão mecânico, custódia limpa de ambiente e auditor independente que reproduz evidência."**

Este documento é a **fonte canônica de autoridade** da Suíte Henke para verificação de entrega, integridade de testes e prevenção de atalhos ("migués") tomados por agentes autônomos de IA. Qualquer skill da suíte que cite verificação ou auditoria subordina-se estritamente às diretrizes aqui estabelecidas.

---

## 1. A Regra de Ouro do Conflito: Portão vs Produto

> **"Quando o portão (teste/linter) e o produto (requisito) conflitam, você conserta o portão — NUNCA satisfaz os dois com truque."**

O modo de falha mais comum de LLMs sob pressão é a **trapaça de conformidade**:
* **O Caso do `.sr-only`**: O produto exige que a interface seja 100% em português, mas o teste legado exige que o texto seja em inglês (`expect(text).toBe("Edit")`). Em vez de atualizar o teste, a IA insere um `<span class="sr-only">Edit</span>` invisível para o teste passar, mantendo o botão em português para o usuário. Isso é fraude técnica: quebra a acessibilidade (leitores de tela anunciam duplicado) e falsifica a cobertura.
* **A Diretriz Obrigatória**: Se o requisito de produto mudou, o portão de teste DEVE ser atualizado para refletir a nova realidade. O teste é o espelho do produto, não um obstáculo a ser contornado.
* **Portões Mecânicos Prioritários**: Regras escritas em prosa são facilmente ignoradas por LLMs em tarefas longas. Projetos sérios devem implementar testes automatizados e linters que falham fisicamente o build (ex: detectores de `.sr-only` duplicado em elementos irmãos).

---

## 2. O Auditor Não Cita Evidência — Ele REPRODUZ

> **"O relatório do implementador é uma alegação, não um fato."**

Quando um agente atua como Auditor ou Revisor (`henke-audit-vibecode`, `henke-debug-sanidade` ou subagente de QA):

1. **Ledger de Comandos Executáveis**: Todo implementador é obrigado a fornecer a lista exata dos comandos executados para validar sua entrega (ex: `cargo test --bin engine`, `npm run test:unit`, `pytest tests/`).
2. **Re-execução Verbatim**: O auditor NUNCA deve aceitar um print ou texto de log colado pelo implementador como prova. O auditor deve abrir o terminal e **re-executar os comandos na íntegra**.
3. **Validação de Payload Real**: O auditor deve inspecionar se os testes verificam o conteúdo e a integridade dos dados gerados, e não apenas se o código retornou status 200 ou saiu com código 0 sem capotar.
4. **Inspeção de DOM Montado vs Regex**: Ao auditar internacionalização (i18n), acessibilidade ou layout, varreduras superficiais por Regex no código-fonte deixam passar falsos negativos (ex: termos interpolados ou importados). A verificação deve ser feita no componente montado ou via teste de integração.

---

## 3. Custódia de Ambiente, Worktree e Binários

Mais da metade dos incidentes graves em projetos assistidos por IA decorrem de desleixo de ambiente, e não de erros de sintaxe:

- **Worktree e Branch Corretas**: Antes de tocar em qualquer arquivo, rode `git branch` e `git status`. Confirme que está na worktree correta da sprint e não no checkout principal ou numa branch morta.
- **Custódia Git Limpa**: 
  - Zero arquivos untracked perdidos.
  - Zero modificações soltas no working tree ao declarar conclusão.
  - Zero commits descolados sem push (quando o usuário solicitou publicação).
- **Resolução de Env e Binários Nativos**: Em projetos híbridos (TypeScript + Rust / Python / C++), NUNCA assuma que o binário está atualizado. Verifique se as variáveis de ambiente (ex: `*_PATH`) estão apontando para o binário recém-compilado na worktree ativa, e não para um executável fóssil gerado dias atrás em outra pasta.

---

## 4. A Seção Obrigatória: "O que NÃO foi verificado"

> **"Afirmar 100% de cobertura quando você não rodou o binário no hardware real ou não testou a UI no navegador é desonestidade técnica."**

Nenhum agente sob a Suíte Henke pode encerrar uma tarefa declarando perfeição cega. Todo relatório de entrega DEVE conter:

1. **O que foi comprovado fisicamente**: Comandos rodados, testes verdes e evidências reproduzidas.
2. **Ressalvas e Limitações Explícitas ("O que NÃO foi verificado")**:
   - Testes que não puderam ser executados por falta de hardware (ex: GPU ausente).
   - APIs de terceiros não invocadas por ausência de credenciais no ambiente.
   - Comportamentos visuais que não puderam ser inspecionados em tela real.
   - TODOs ou débitos técnicos conscientemente introduzidos para a próxima etapa.

---

## 5. O Modelo dos Dois Papéis (Separation of Concerns)

Para projetos complexos, a melhor prática de engenharia é a separação rígida de agentes:

* **Agente 1: O Construtor (Builder)**: Utiliza as skills cirúrgicas (`henke-code-cirurgico`, `henke-code-ponte`, `henke-blindagem`) para implementar com foco absoluto, gerando o ledger de comandos.
* **Agente 2: O Perito Independente (Auditor Adversarial)**: Em uma nova sessão ou subagente independente, invoca `henke-audit-vibecode` para auditar a custódia, reproduzir os testes verbatim e caçar atalhos ou armadilhas.

---

## 6. Checklist Rápido de Pré-Conclusão

Antes de declarar qualquer entrega como finalizada, valide:

- [ ] **Entrega Real (Zero Stubs)**: Nenhuma função vazia, mock permanente ou comentário delegando a solução para depois.
- [ ] **Portão vs Produto**: Nenhuma asserção contornada por truques de estilo ou elementos ocultos.
- [ ] **Ledger Executável**: Comandos de teste documentados e reproduzíveis.
- [ ] **Custódia Limpa**: `git status` limpo, worktree e branch corretas, variáveis de ambiente validadas.
- [ ] **Ressalvas Transparentes**: Seção "O que NÃO foi verificado" preenchida com honestidade técnica.
