# TEST_READY: Wave 6 Production Expansions E2E Test Suite

**Author**: E2E Test Suite Author (`test_writer_e2e`)  
**Date**: 2026-11-08  
**Repository Root**: `F:\DEV\ACTIVE\henke-skills-w6`  
**Test Suite Directory**: `tests/e2e/`  
**Test Framework**: Standard Python `unittest`  
**Target Platform**: Windows (cmd.exe / PowerShell / Git Bash)  

---

## 1. Test Runner Command

To discover and execute the entire opaque-box E2E test suite:

```powershell
# From the repository root (F:\DEV\ACTIVE\henke-skills-w6):
python -m unittest discover -s tests/e2e -v
```

To run individual tiers:
```powershell
# Tier 1: Feature Coverage (>=5 tests per feature for R1-R6)
python -m unittest tests/e2e/test_wave6_e2e_tier1.py -v

# Tier 2: Boundary & Corner Cases (>=5 tests per feature for R1-R6)
python -m unittest tests/e2e/test_wave6_e2e_tier2.py -v

# Tier 3: Cross-Feature Interactions
python -m unittest tests/e2e/test_wave6_e2e_tier3.py -v

# Tier 4: Real-World Application Scenarios
python -m unittest tests/e2e/test_wave6_e2e_tier4.py -v
```

---

## 2. Test Suite Architecture & Methodology

The E2E test suite is constructed as a rigorous, opaque-box validation suite following the 4-tier testing hierarchy:
1. **Tier 1 — Feature Coverage**: Comprehensive requirement-driven verification covering the primary happy path and error paths for all 6 requirements (R1 to R6).
2. **Tier 2 — Boundary & Corner Cases**: Stress-testing boundaries, corrupted/malformed inputs, 0-timeouts, extreme RAM limits, non-git environments, and Windows filesystem edge conditions.
3. **Tier 3 — Cross-Feature Interactions**: Pairwise and combinatorial interactions ensuring subsystems operate harmoniously (e.g. mission classifier + gate cache, pre-commit hook + process runner taskkill, 500 MB RAM eviction + HTML report generation).
4. **Tier 4 — Real-World Application Scenarios**: End-to-end multi-step developer workflows simulating full project lifecycles from fresh initialization to polyglot gate checks and executive reporting.

---

## 3. Tier Summary & Counts

| Tier | Module | Test Cases | Scope |
|---|---|:---:|---|
| **Tier 1** | `tests/e2e/test_wave6_e2e_tier1.py` | 38 | Feature Coverage (R1: 10, R2: 7, R3: 5, R4: 5, R5: 6, R6: 5) |
| **Tier 2** | `tests/e2e/test_wave6_e2e_tier2.py` | 30 | Boundary & Corner Cases (R1: 5, R2: 5, R3: 5, R4: 5, R5: 5, R6: 5) |
| **Tier 3** | `tests/e2e/test_wave6_e2e_tier3.py` | 6 | Pairwise Cross-Feature Interactions |
| **Tier 4** | `tests/e2e/test_wave6_e2e_tier4.py` | 5 | Real-World Developer Scenarios |
| **Total** | `tests/e2e/` | **79** | **Full Opaque-Box E2E Suite** |

---

## 4. Feature Coverage Matrix (R1 to R6)

| Req | Capability | Tier 1 | Tier 2 | Tier 3 | Tier 4 | Total Tests | Status |
|:---:|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **R1** | Mission Classifier & Multi-Framework Bridges (10 stacks + 3 frontend tools, selective filtering, interactive prompt, extensibility) | 10 | 5 | 3 | 2 | **20** | Implemented & Ready |
| **R2** | Native Pre-Commit Hook of 1 Command (`henke install-hook`, idempotency, legacy backup, gate enforcement) | 7 | 5 | 1 | 1 | **14** | Implemented & Ready |
| **R3** | Windows Atomic Zombie Process Elimination (`taskkill /F /T`, process tree killing, race code 128) | 5 | 5 | 2 | 1 | **13** | Implemented & Ready |
| **R4** | Self-Contained Local HTML Dashboard (`henke report`, zero external CDNs, mandatory plain-language summary) | 5 | 5 | 2 | 2 | **14** | Implemented & Ready |
| **R5** | Smart Gate Cache with 500 MB RAM Ceiling (Merkle keys, cache hits, Win32 RAM tracking, LRU eviction) | 6 | 5 | 2 | 1 | **14** | Implemented & Ready |
| **R6** | Strict Isolation & Windows Platform (Worktree boundary, mixed slashes, UTF-8 streams, clean tempdir teardown) | 5 | 5 | 1 | 1 | **12** | Implemented & Ready |

---

## 5. Detailed Test Inventory

### Tier 1: Feature Coverage (`tests/e2e/test_wave6_e2e_tier1.py`)
- `test_r1_1_python_stack_detection`: Detects Python stack via `pyproject.toml`, `requirements.txt`, and `.py`.
- `test_r1_2_javascript_typescript_stack_detection`: Detects JS/TS stack via `package.json` and `tsconfig.json`.
- `test_r1_3_rust_go_java_detection`: Detects compiled languages (Rust `Cargo.toml`, Go `go.mod`, Java `pom.xml`).
- `test_r1_4_c_csharp_php_ruby_swift_detection`: Detects C/C++, C# (.NET), PHP, Ruby, Kotlin/Swift.
- `test_r1_5_svelte_vite_tailwind_tooling_detection`: Detects frontend tooling (Svelte, Vite, Tailwind).
- `test_r1_6_selective_gate_filtering`: Executes gates only for identified stacks.
- `test_r1_7_unconfigured_stack_bypass_non_interactive`: Non-interactive bypass for unconfigured stacks.
- `test_r1_8_unconfigured_stack_interactive_prompt_accept`: User accepting prompt enables stack in `.henke/CONFIG.json`.
- `test_r1_9_unconfigured_stack_interactive_prompt_decline`: User declining prompt leaves stack disabled.
- `test_r1_10_extensibility_documentation`: Verifies `docs/BRIDGES.md` extensibility documentation.
- `test_r2_1_install_hook_basic_creation`: Installs `.git/hooks/pre-commit` with `# HENKE-PRE-COMMIT-HOOK`.
- `test_r2_2_install_hook_idempotency`: Multiple installations overwrite cleanly without duplicate blocks.
- `test_r2_3_install_hook_legacy_backup_without_force`: Foreign hook backed up to `.git/hooks/pre-commit.legacy`.
- `test_r2_4_install_hook_force_overwrite`: `--force` flag directly overwrites foreign hook.
- `test_r2_5_install_hook_rejects_non_git_repo`: Fails with clear message when target is not a git repo.
- `test_r2_6_hook_execution_blocks_commit_on_failure`: Pre-commit hook rejects git commit when gate checks fail.
- `test_r2_7_hook_execution_allows_commit_on_pass`: Pre-commit hook allows git commit when gate checks pass.
- `test_r3_1_normal_execution_success`: Normal fast command returns code 0 and stdout.
- `test_r3_2_timeout_triggers_taskkill_windows`: Subprocess timeout invokes `taskkill /F /T` and sets `timed_out=True`.
- `test_r3_3_process_tree_termination_no_orphans`: Multilevel process tree terminated without orphan zombies.
- `test_r3_4_race_condition_exit_code_128_handling`: Race condition when process dies just before taskkill handled cleanly.
- `test_r3_5_process_execution_result_contract`: Validates `ProcessExecutionResult` dataclass attributes.
- `test_r4_1_report_generation_default_location`: Generates report at `.henke/report.html`.
- `test_r4_2_report_generation_custom_output`: Generates report at custom path specified via `--out`.
- `test_r4_3_report_self_contained_zero_external_cdns`: 100% offline self-contained HTML (zero external scripts/fonts/styles).
- `test_r4_4_report_mandatory_plain_language_section`: Mandatory plain-language executive summary section present.
- `test_r4_5_report_visual_audit_status_cards`: Visual audit results and gate status information rendered cleanly.
- `test_r5_1_cache_key_determinism`: Deterministic SHA-256 Merkle cache key from script, args, and target files.
- `test_r5_2_cache_hit_and_retrieve`: Stored gate result retrieved accurately on subsequent queries.
- `test_r5_3_cache_invalidation_on_file_mutation`: Mutating target file changes cache key.
- `test_r5_4_ram_measurement_win32`: RAM measurement on Windows queries Win32 psapi returning working set MB.
- `test_r5_5_ram_eviction_when_limit_exceeded`: `enforce_ram_limit` evicts LRU entries when RAM exceeds ceiling.
- `test_r5_6_cache_miss_returns_none`: Querying nonexistent cache key returns None without error.
- `test_r6_1_worktree_isolation_boundary`: Verifies all operations are confined within the repository sandbox.
- `test_r6_2_path_traversal_rejection`: Path traversal attempts with `../` are safely confined.
- `test_r6_3_windows_path_normalization`: Handles mixed forward and backward slashes on Windows.
- `test_r6_4_utf8_special_characters`: Special Portuguese characters handled without encoding crashes.
- `test_r6_5_safe_cleanup_no_locked_files`: Sandboxes clean up without `[WinError 32]` file locking issues.

### Tier 2: Boundary & Corner Cases (`tests/e2e/test_wave6_e2e_tier2.py`)
- `test_t2_r1_empty_repository`: Empty or documentation-only repo returns empty stack list without crashing.
- `test_t2_r1_pruned_directories_ignored`: Manifests in `node_modules`, `.git`, `dist`, `target` are pruned.
- `test_t2_r1_corrupted_manifest_files`: Malformed `package.json` handled without unhandled exception.
- `test_t2_r1_ambiguous_build_files`: `Makefile` in pure Go repo does not misclassify as C/C++.
- `test_t2_r1_case_insensitivity_windows`: Mixed casing in manifest file names handled on Windows NTFS.
- `test_t2_r2_missing_git_directory`: Non-git directory fails gracefully with exit code 2.
- `test_t2_r2_empty_or_whitespace_hook`: Pre-existing empty or whitespace hook replaced safely.
- `test_t2_r2_force_flag_without_prior_hook`: Force flag works normally even when no prior hook exists.
- `test_t2_r2_custom_hooks_path_resolution`: Resolves git hooks directory dynamically when `core.hooksPath` is set.
- `test_t2_r2_submodule_or_worktree_hooks_resolution`: Hook installation resolves when `.git` is a worktree file.
- `test_t2_r3_zero_or_submillisecond_timeout`: Zero or submillisecond timeout triggers immediate taskkill.
- `test_t2_r3_massive_stdout_buffer_saturation`: Process emitting 5MB of stdout before timeout does not deadlock pipe.
- `test_t2_r3_nonexistent_executable`: Nonexistent binary raises FileNotFoundError or returns non-zero code handled gracefully.
- `test_t2_r3_string_command_execution`: Accepts string command in addition to list of tokens.
- `test_t2_r3_already_dead_process_on_timeout`: Taskkill on already dead process handled cleanly (code 128).
- `test_t2_r4_empty_henke_directory`: Report generation on empty `.henke` generates clean empty state dashboard.
- `test_t2_r4_nonexistent_output_parent_directory`: Creates parent directories automatically for nested output paths.
- `test_t2_r4_huge_volume_of_findings`: 500+ audit findings render cleanly without memory bloat.
- `test_t2_r4_html_escaping_and_xss_protection`: Gate messages containing `<script>` or special chars are HTML-escaped.
- `test_t2_r4_corrupted_runs_json_graceful_handling`: Corrupted JSON in runs directory does not crash report generation.
- `test_t2_r5_corrupted_cache_file_recovery`: Corrupted cache JSON on disk safely discarded and replaced.
- `test_t2_r5_zero_or_low_ram_limit_evicts_all`: Setting RAM limit to 0 MB forces eviction of all entries.
- `test_t2_r5_empty_key_or_empty_dict`: Empty key or empty dict handled gracefully.
- `test_t2_r5_rapid_sequential_cache_access`: 50 consecutive store and get operations maintain consistency.
- `test_t2_r5_atomic_write_replacement`: Atomic file replacement avoids partial read states.
- `test_t2_r6_spaces_in_directory_paths`: Repositories located in paths with spaces work without splitting.
- `test_t2_r6_unicode_file_paths`: Files with non-ASCII and accented characters operate cleanly.
- `test_t2_r6_relative_and_absolute_equivalence`: Canonical resolution equates relative and absolute paths.
- `test_t2_r6_mixed_slashes_in_cli_args`: CLI handles mixed forward and backslashes in paths.
- `test_t2_r6_large_ledger_stream_parsing`: Parsing ledger with 5,000 lines streams without memory bloat.

### Tier 3: Cross-Feature Interactions (`tests/e2e/test_wave6_e2e_tier3.py`)
- `test_t3_1_classifier_and_gate_cache_pipeline`: Stacks detected, gates cached; partial mutation invalidates only mutated stack.
- `test_t3_2_install_hook_and_taskkill_timeout`: Pre-commit hook handles hanging gate command via taskkill without hanging git commit.
- `test_t3_3_interactive_prompt_and_config_update`: Interactive prompt enables stack in `CONFIG.json`, making it active for subsequent filtering.
- `test_t3_4_cache_eviction_under_ram_pressure_and_report_generation`: High RAM load evicts cache LRU entries; report generator completes with full ledger.
- `test_t3_5_taskkill_timeout_and_audit_ledger_hashing`: Taskkill timeout during execution generates proper hashable output for ledger.
- `test_t3_6_polyglot_repo_full_check_and_report`: Polyglot repo (Python + Vite + Tailwind) detection and report generation.

### Tier 4: Real-World Application Scenarios (`tests/e2e/test_wave6_e2e_tier4.py`)
- `test_t4_scenario_1_polyglot_full_lifecycle`: Full workflow on multi-stack polyglot repo with hook, commit rejection/acceptance, and report.
- `test_t4_scenario_2_runaway_deadlock_elimination`: Runaway hanging subprocess deadlock eliminated atomically via taskkill on Windows.
- `test_t4_scenario_3_progressive_stack_adoption`: Progressive stack adoption in fresh repo with interactive prompt and config update.
- `test_t4_scenario_4_high_frequency_dev_loop_cache_and_memory`: High-frequency dev loop with cache hits and 500 MB RAM eviction under load.
- `test_t4_scenario_5_full_audit_and_executive_reporting`: Full security audit report generation with self-contained HTML and plain-language summary.

---

## 6. Progressive Testability Verification

The test suite strictly adheres to the Progressive Testability protocol:
- Tests discoverable and verifiable via standard `unittest discover`.
- As each milestone (M1 through M5) completes its implementation, corresponding tests automatically transition from skipped to active and pass.
- Current pre-implementation baseline:
  - Total tests discovered: **79**
  - Executed & passed (isolation, platform, git mechanics, buffer streaming, Win32 memory): **13**
  - Awaiting milestone implementation: **66**
  - Errors / Failures: **0**
