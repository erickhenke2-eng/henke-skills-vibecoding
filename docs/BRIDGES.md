# Guia de Pontes Multi-Framework e Extensibilidade (`docs/BRIDGES.md`)

Este documento especifica a arquitetura do **Classificador de Missão e Pontes Multi-Framework (Henke Toolkit Wave 6 / PROTOCOLO v4.0)** e fornece o manual técnico completo para desenvolvedores e equipes adaptarem as detecções, registrarem novas linguagens e customizarem os portões mecânicos e test runners do toolkit.

---

## 1. Visão Geral da Arquitetura

O Classificador de Missão (`henke/mission_classifier.py`) atua como a primeira camada de inteligência do Henke Toolkit ao inspecionar um repositório. Em vez de rodar portões de forma estática e cega (o que geraria falsos-positivos ou desperdício de tempo em repositórios heterogêneos), o classificador:

1. **Varre Heuristicamente o Repositório**: Detecta linguagens, frameworks e ferramentas de build através de manifestos canônicos, extensões de arquivo e padrões de projeto, podando diretórios de build e dependências (`node_modules`, `target`, `dist`, `.venv`, `.git`).
2. **Cruza com a Configuração do Projeto (`.henke/CONFIG.json`)**: Identifica quais stacks estão ativas/habilitadas e quais são stacks recém-introduzidas ainda não configuradas.
3. **Gerencia Ciclo Interativo vs. CI/CD**:
   - Em sessões interativas (`sys.stdin.isatty() == True`), questiona o desenvolvedor (`Deseja configurar a nova stack '<nome>' antes de continuar? [s/N]: `) antes de ativar auditorias para uma tecnologia nova.
   - Em pipelines de CI/CD não-interativos, ignora a auditoria da stack desconhecida com aviso explícito no log, prevenindo quebras inesperadas de build.
4. **Dispara Portões Mecânicos Seletivos**: Aciona exclusivamente os portões aplicáveis (ex: `assertless_scan` para runners de teste, `a11y_scan` para Svelte/Tailwind, `hazard_scan` universal).

---

## 2. Pontes Nativas Integradas (10 Linguagens + 3 Ferramentas)

A distribuição padrão do Henke Toolkit inclui 13 pontes nativas pré-configuradas:

| ID da Stack | Nome da Stack | Categoria | Assinaturas Primárias (Manifestos / Extensões) | Test Runner Padrão | Portões Mecânicos Padrão |
|---|---|---|---|---|---|
| `python` | Python | `language` | `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`, `environment.yml`, `*.py`, `*.pyi` | `pytest` | `assertless`, `hazard`, `mutant` |
| `typescript` | TypeScript | `language` | `tsconfig.json`, `*.ts`, `*.tsx` | `npm test` | `assertless`, `hazard`, `a11y` |
| `javascript` | JavaScript | `language` | `package.json`, `jsconfig.json`, `deno.json`, `bun.lockb`, `*.js`, `*.jsx`, `*.mjs`, `*.cjs` | `npm test` | `assertless`, `hazard`, `a11y` |
| `rust` | Rust | `language` | `Cargo.toml`, `Cargo.lock`, `*.rs` | `cargo test` | `assertless`, `hazard` |
| `go` | Go | `language` | `go.mod`, `go.sum`, `go.work`, `*.go` | `go test ./...` | `assertless`, `hazard` |
| `java` | Java | `language` | `pom.xml`, `build.gradle`, `settings.gradle`, `*.java` | `mvn test` | `assertless`, `hazard` |
| `cpp` | C / C++ | `language` | `CMakeLists.txt`, `meson.build`, `vcpkg.json`, `conanfile.txt`, `*.cpp`, `*.c`, `*.h`, `Makefile` (com fontes C/C++) | `ctest --output-on-failure` | `hazard` |
| `csharp` | C# (.NET) | `language` | `*.csproj`, `*.sln`, `global.json`, `Directory.Build.props`, `*.cs` | `dotnet test` | `assertless`, `hazard` |
| `php` | PHP | `language` | `composer.json`, `composer.lock`, `phpunit.xml`, `*.php` | `vendor/bin/phpunit` | `assertless`, `hazard` |
| `ruby` | Ruby | `language` | `Gemfile`, `Gemfile.lock`, `Rakefile`, `*.gemspec`, `*.rb` | `bundle exec rspec` | `assertless`, `hazard` |
| `kotlin_swift` | Kotlin / Swift | `language` | `build.gradle.kts` (com plugin Kotlin), `Package.swift`, `*.xcodeproj`, `*.kt`, `*.swift` | `./gradlew test` / `swift test` | `hazard` |
| `svelte` | Svelte / SvelteKit | `framework` | `svelte.config.*`, `*.svelte` | `npx vitest run` | `a11y` |
| `vite` | Vite Tooling | `tooling` | `vite.config.*` | `npx vitest run` | `metaconfig`, `hazard` |
| `tailwind` | Tailwind CSS | `tooling` | `tailwind.config.*` | *(build check)* | `a11y` |

---

## 3. Estrutura de Configuração do Projeto (`.henke/CONFIG.json`)

O arquivo `.henke/CONFIG.json` armazena o estado das stacks e as preferências de auditoria do repositório.

### Schema JSON Oficial

```json
{
  "$schema": "https://raw.githubusercontent.com/henke-toolkit/schemas/v4.0/config.schema.json",
  "versao_protocolo": "4.0",
  "stacks": {
    "python": {
      "enabled": true,
      "configured": true,
      "test_cmd": "python -m unittest discover -s tests",
      "gates": ["assertless", "hazard", "mutant"]
    },
    "typescript": {
      "enabled": true,
      "configured": true,
      "test_cmd": "npm test",
      "gates": ["assertless", "hazard", "a11y"]
    },
    "svelte": {
      "enabled": true,
      "configured": true,
      "gates": ["a11y"]
    },
    "ruby": {
      "enabled": false,
      "configured": false,
      "reason": "user_declined"
    }
  }
}
```

### Campos de Cada Entrada em `stacks`:
- `enabled` (`boolean`): Se `true`, os portões daquela stack são disparados durante `henke check`.
- `configured` (`boolean`): Se `true`, a stack já passou pelo crivo de configuração e não emitirá prompts de nova tecnologia.
- `test_cmd` (`string | null`): Comando de teste específico para executar durante auditorias profundas.
- `gates` (`list[string]`): Lista de portões mecânicos vinculados à tecnologia (`hazard`, `assertless`, `a11y`, `metaconfig`, `contracts`, `mutant`).
- `reason` (`string`, opcional): Justificativa para desabilitação (ex: `"user_declined"`).

---

## 4. Como Criar e Registrar Novas Pontes Customizadas

Usuários externos podem estender o classificador para qualquer linguagem ou framework sem modificar uma única linha do código-fonte do Henke Toolkit.

Basta adicionar arquivos de definição JSON no diretório `.henke/bridges/` dentro do seu repositório:
```
meu-projeto/
├── .henke/
│   ├── CONFIG.json
│   └── bridges/
│       ├── elixir.json
│       └── flutter.json
```

### Schema de uma Ponte Customizada (`.henke/bridges/<nome>.json`)

```json
{
  "id": "elixir",
  "name": "Elixir / Phoenix",
  "language": "elixir",
  "category": "language",
  "detection": {
    "files": ["mix.exs", "mix.lock"],
    "extensions": [".ex", ".exs"]
  },
  "test_runner": {
    "cmd": "mix test",
    "discovery_regex": "Finished in \\d+\\.\\d+ seconds \\((\\d+) tests?\\)",
    "fail_regex": "(\\d+) failures?"
  },
  "gates": ["hazard", "assertless"]
}
```

### Exemplo 2: Ponte para Dart / Flutter

```json
{
  "id": "flutter",
  "name": "Dart / Flutter Mobile",
  "language": "dart",
  "category": "framework",
  "detection": {
    "files": ["pubspec.yaml", "pubspec.lock"],
    "extensions": [".dart"]
  },
  "test_runner": {
    "cmd": "flutter test"
  },
  "gates": ["hazard"]
}
```

### Exemplo 3: Ponte para Scala / sbt

```json
{
  "id": "scala",
  "name": "Scala / sbt",
  "language": "scala",
  "category": "language",
  "detection": {
    "files": ["build.sbt"],
    "extensions": [".scala", ".sc"]
  },
  "test_runner": {
    "cmd": "sbt test"
  },
  "gates": ["hazard", "assertless"]
}
```

---

## 5. Pruning de Diretórios e Prevenção de Falsos-Positivos

Para garantir alta velocidade e evitar que artefatos compilados ou dependências de terceiros gerem detecções espúrias, o scanner aplica **poda agressiva imediata** nos seguintes diretórios:

- Controle de versão: `.git`
- Gerenciadores de pacote: `node_modules`, `vendor`, `.venv`, `venv`
- Compilação e binários: `dist`, `build`, `target`, `bin`, `obj`, `__pycache__`
- Metadados de ferramentas: `.henke`, `.idea`, `.vscode`, `.tox`, `.pytest_cache`, `.mypy_cache`

Além disso, a profundidade máxima de busca no sistema de arquivos é limitada a 4 níveis, garantindo execução em menos de 50ms mesmo em repositórios com dezenas de milhares de arquivos.

---

## 6. Uso Programático via Python API

O classificador expõe contratos formais limpos para integração em outras ferramentas ou scripts customizados:

```python
from pathlib import Path
from henke.mission_classifier import (
    detect_stacks,
    classify_repository,
    filter_gates_for_stacks,
    prompt_unconfigured_stack,
)

repo = Path(".")

# 1. Detectar stacks físicas no repositório
stacks = detect_stacks(repo)
for s in stacks:
    print(f"Detectado: {s.name} ({s.language}) - {len(s.detected_files)} arquivos")

# 2. Classificação completa (cruzando com .henke/CONFIG.json)
classification = classify_repository(repo)
print(f"Stacks configuradas: {classification.configured_stacks}")
print(f"Stacks não configuradas: {[s.name for s in classification.unconfigured_stacks]}")
print(f"Portões ativos a executar: {classification.active_gates}")

# 3. Filtragem de portões para execução
active, ignored = filter_gates_for_stacks(stacks, configured=["python", "rust"])
```

---

## 7. Diagnóstico e Inspeção via CLI

Você pode inspecionar diretamente a classificação do seu projeto através do comando:

```bash
# Diagnóstico amigável
python henke/mission_classifier.py --repo .

# Diagnóstico em formato JSON estruturado
python henke/mission_classifier.py --repo . --json

# Diagnóstico integrado na CLI Henke
python henke/henke_cli.py doctor --repo . --json
```
