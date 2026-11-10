# Design Spec — Wave 6: Expansões de Produção Henke Toolkit

**Spec-hash binding:** este documento é a spec de referência da Wave 6 para `spec_drift.py` e `contract_closure.py`.

## 1. Contexto & Problema
A versão 4.0 do Henke Toolkit estabeleceu portões mecânicos e auditoria adversarial independente. Contudo, em ambientes reais de desenvolvimento no Windows:
1. Repositórios usam múltiplos frameworks e linguagens diferentes (Python, TypeScript, Rust, Go, Java, C#, Svelte, Vite, Tailwind), necessitando de detecção dinâmica e portões cirúrgicos.
2. Desenvolvedores esquecem de rodar os portões antes de commitar, gerando quebras no CI; hooks externos como Husky exigem Node.js em repositórios que não usam JavaScript.
3. Testes cancelados ou com timeout deixavam processos zumbis rodando em background no Windows travando portas de rede e arquivos.
4. Relatórios técnicos eram ininteligíveis para tomadores de decisão não-técnicos.
5. Execuções repetidas de portões lentos sem cache consumiam CPU, ou acumulavam memória sem limites de RAM.

## 2. Decisões Técnicas & Arquitetura
1. **R1: Classificador de Missão & Pontes Multi-Framework** (`scripts/mission_classifier.py`):
   - Detecção baseada em assinaturas de manifesto e extensões de arquivos em profundidade controlada (excluindo diretórios de dependências).
   - Suporte pronto para 10 linguagens (Python, TS/JS, Rust, Go, Java, C/C++, C#, PHP, Ruby, Kotlin/Swift) + Svelte, Vite, Tailwind.
   - Guia de extensão para usuários em `docs/BRIDGES.md`.
2. **R2: Pre-Commit Hook Nativo** (`scripts/install_hook.py`):
   - Instalação nativa em `.git/hooks/pre-commit` com caminho absoluto autônomo.
   - Preservação de hooks legados pré-existentes com backup timestampado (`pre-commit.legacy.<timestamp>`) e encadeamento de execução.
3. **R3: Morte Atômica de Processos Zumbis** (`scripts/process_runner.py`):
   - Execução via `subprocess.Popen` e terminação imediata de árvore completa no Windows com `taskkill /F /T /PID <pid>` no timeout.
4. **R4: Dashboard Visual Local em HTML Autocontido** (`scripts/report_generator.py`):
   - Arquivo HTML 100% offline (zero CDNs externas) com estilos inline.
   - Seção obrigatória de Resumo Executivo em Linguagem Simples para leigos.
   - Veredito rigoroso: qualquer portão com falha (`gates_failed > 0`) marca REPROVADO.
5. **R5: Cache Inteligente de Portões com Limite de RAM** (`scripts/gate_cache.py`):
   - Hashing Merkle determinístico incluindo diretórios de CI (`.github`) e arquivos do repositório.
   - Medição real do consumo de memória privada/working set no Windows com Win32 API.
   - Evicção LRU em memória e remoção correspondente no disco `.henke/cache/` quando o limite de 500 MB é atingido.

## 3. Contratos de Tipos, Entrada & Saída
- `scripts/mission_classifier.py`:
  - `classify_repository(repo_path: Path) -> MissionClassification`
  - `detect_stacks(repo_path: Path) -> List[StackInfo]`
  - `prompt_unconfigured_stack(stack: str, config_path: Path, interactive: bool = False) -> bool`
- `scripts/process_runner.py`:
  - `run_process(cmd: List[str] | str, cwd: Path, timeout_sec: float | None = None, env: dict | None = None) -> ProcessExecutionResult`
- `scripts/install_hook.py`:
  - `install_hook(repo_path: Path, force: bool = False) -> Tuple[bool, str]`
- `scripts/gate_cache.py`:
  - `compute_cache_key(gate_name: str, script_path: Path, args: List[str], repo_root: Path) -> str`
  - `get_cached_gate(key: str, repo_root: Path | None = None) -> Dict[str, Any] | None`
  - `store_cached_gate(key: str, payload: Dict[str, Any], repo_root: Path | None = None) -> None`
  - `enforce_ram_limit(limit_mb: float = 500.0, repo_root: Path | None = None, force_flush: bool = False) -> int`
- `scripts/report_generator.py`:
  - `generate_report(repo_path: Path, output_file: Path | None = None) -> Path`

## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)
- `.gitignore`
- `TEST_READY.md`
- `docs/BRIDGES.md`
- `docs/designs/wave6_production_expansions.md`
- `scripts/audit_ledger.py`
- `scripts/gate_cache.py`
- `scripts/henke_cli.py`
- `scripts/install_hook.py`
- `scripts/mission_classifier.py`
- `scripts/process_runner.py`
- `scripts/report_generator.py`
- `scripts/run_state.py`
- `scripts/metricas/mutant_hunt.py`
- `tests/test_gate_cache.py`
- `tests/test_install_hook.py`
- `tests/test_mission_classifier.py`
- `tests/test_process_runner.py`
- `tests/test_report.py`
- `tests/e2e/__init__.py`
- `tests/e2e/e2e_common.py`
- `tests/e2e/test_wave6_e2e_tier1.py`
- `tests/e2e/test_wave6_e2e_tier2.py`
- `tests/e2e/test_wave6_e2e_tier3.py`
- `tests/e2e/test_wave6_e2e_tier4.py`

## 5. Pontes de Contrato
- ponte: `scripts/process_runner.py::run_process -> tests/test_process_runner.py`
- ponte: `scripts/install_hook.py::install_hook -> tests/test_install_hook.py`
- ponte: `scripts/mission_classifier.py::classify_repository -> tests/test_mission_classifier.py`
- ponte: `scripts/gate_cache.py::compute_cache_key -> tests/test_gate_cache.py`
- ponte: `scripts/report_generator.py::generate_report -> tests/test_report.py`

## 6. Casos de Borda & Matriz Pre-Mortem de 40 Anos (Estadista)
1. **Queda de Energia / Crash Parcial** (`[MECÂNICO]`): Escrita atômica em arquivos de cache e relatórios via arquivos `.tmp` e `os.replace`.
2. **Escala 100x / Efeito Tornado** (`[MECÂNICO]`): Teto rígido de 500 MB RAM com monitoramento de memória física/privada e evicção LRU em memória e disco.
3. **Direção de Dependência (3 Gavetas)** (`[MECÂNICO]`): Scripts de portão e executores operam em isolamento sem importar camadas de apresentação.
4. **Evolução & Migração de Estado** (`[MECÂNICO]`): Hooks pré-existentes são preservados com versionamento timestampado e executados em cadeia.
5. **Fronteira de Falsificabilidade** (`[MECÂNICO: canários bilaterais]`): Testes de mutação, canários de timeout e rejeição estrita de commits e relatórios inválidos.

## 7. Ledger Esperado de Comandos
- `python -m unittest discover -s tests -p "test_*.py"`
- `python scripts/metricas/hazard_scan.py .`
- `python scripts/metricas/assertless_scan.py tests/`
- `python scripts/metricas/a11y_scan.py .`
- `python scripts/henke_cli.py check --repo .`
