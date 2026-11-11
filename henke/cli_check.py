"""cli_check.py — `henke check`/`gate`: bateria de portões com cache, manifesto e agregação."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from henke.cli_kernel import (
    GATE_MAP,
    SCRIPTS_DIR,
    SUITE_ROOT,
)
from henke.gate_cache import (
    clear_cache,
    compute_cache_key,
    enforce_ram_limit,
    get_cached_gate,
    store_cached_gate,
)
from henke.process_runner import run_process



def _cmd_gate(gate_name: str, extra_args: list[str], repo_root: Path = Path(".")) -> int:
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    script_path = GATE_MAP.get(gate_name)
    if not script_path or not script_path.is_file():
        print(
            f"ERRO: portão desconhecido '{gate_name}'. Disponíveis: {', '.join(sorted(GATE_MAP))}",
            file=sys.stderr,
        )
        return 2

    no_cache = False
    args: list[str] = []
    for a in extra_args:
        if a == "--no-cache":
            no_cache = True
        elif a == "--clear-cache":
            clear_cache(repo_root)
            # DS-19: limpar e sair 0 SEM executar o portão parecia verde —
            # o operador pedia veredito fresco e recebia nada. Limpa e SEGUE
            # para a execução (equivalente a --no-cache nesta rodada).
            no_cache = True
            print("[OK] Cache de portões limpo — executando portão.")
        else:
            args.append(a)

    if not args and gate_name in ("hazard", "a11y", "assertless"):
        if gate_name == "assertless" and (repo_root / "tests").is_dir():
            args = [str(repo_root / "tests")]
        else:
            args = [str(repo_root)]

    cache_key = compute_cache_key(gate_name, script_path, args, repo_root)
    if not no_cache:
        cached = get_cached_gate(cache_key, repo_root=repo_root)
        if cached is not None:
            code = cached.get("exit_code", cached.get("returncode", 0))
            print(f"[CACHE HIT] {gate_name} (exit {code})")
            if cached.get("stdout"):
                sys.stdout.write(cached["stdout"])
                if not cached["stdout"].endswith("\n"):
                    sys.stdout.write("\n")
            if cached.get("stderr"):
                sys.stderr.write(cached["stderr"])
                if not cached["stderr"].endswith("\n"):
                    sys.stderr.write("\n")
            return code

    res = run_process([sys.executable, "-Es", str(script_path), *args], cwd=str(repo_root), timeout_sec=120.0)
    out_text = res.stdout or ""
    err_text = res.stderr or ""
    if out_text:
        sys.stdout.write(out_text)
    if err_text:
        sys.stderr.write(err_text)

    store_cached_gate(
        cache_key,
        {
            "gate": gate_name,
            "exit_code": res.returncode,
            "returncode": res.returncode,
            "stdout": out_text,
            "stderr": err_text,
            "passed": res.returncode == 0 and not res.timed_out,
        },
        repo_root=repo_root,
    )
    enforce_ram_limit(500.0, repo_root=repo_root)
    return res.returncode


def _format_manifest_output(scanner_id: str, res) -> str:
    lines = []
    for f in res.findings:
        loc = f.get("location", "?")
        scope = f.get("scope", "")
        lines.append(f"{f.get('rule','?'):<26} {loc}  [{scope}]  {f.get('message','')}")
    lines.append(
        f"{len(res.scanned_files)} file(s) scanned this round, "
        f"{len(res.reused_files)} reused, {len(res.findings)} finding(s)"
    )
    return "\n".join(lines)


def _mirror_event(repo_root: Path, kind: str, payload: dict) -> None:
    """P1-03: espelha veredito no event store v2 — SOMENTE se o state dir já
    existe. `henke check` em repo sem `.henke/` (ANALISAR) não pode criar
    arquivos; com HENKE_STATE_DIR/--state-dir o espelho vai p/ fora do repo."""
    try:
        from henke.state_paths import state_dir
        sdir = state_dir(repo_root)
        if not sdir.is_dir():
            if not os.environ.get("HENKE_STATE_DIR"):
                return
            sdir.mkdir(parents=True, exist_ok=True)
        from henke.event_store import append_event
        append_event(repo_root, kind, payload)
    except Exception as exc:
        print(f"AVISO: event_store mirror falhou: {exc}", file=sys.stderr)


def _resolve_spec_anchor(repo_root: Path, spec_path: str) -> tuple[str | None, str | None]:
    """Âncora de aprovação da spec: (spec_sha256, head_start) da run mais
    recente que declarou ESTE spec_path — sidecar local (fora do repo) é a
    fonte preferida, o run file é mutável. Quando invocado de forma avulsa
    (`henke check --spec <spec.md>` sem run ativa), calcula o hash canônico
    da spec em disco e usa gate-baseline.json ou HEAD para que o diff de
    escopo execute."""
    try:
        spec_abs = Path(spec_path)
        if not spec_abs.is_absolute():
            spec_abs = repo_root / spec_abs
        spec_abs = spec_abs.resolve()
    except OSError:
        return None, None

    def _matches_spec(sp: object) -> bool:
        if not sp or not isinstance(sp, str):
            return False
        try:
            sp_abs = Path(sp)
            if not sp_abs.is_absolute():
                sp_abs = repo_root / sp_abs
            return sp_abs.resolve() == spec_abs
        except OSError:
            return False

    from henke.state_paths import state_dir
    run_dirs = []
    for d in (state_dir(repo_root) / "runs", repo_root / ".henke" / "runs"):
        try:
            dr = d.resolve()
        except OSError:
            continue
        if dr not in run_dirs and dr.is_dir():
            run_dirs.append(dr)
    candidates: list[Path] = []
    for rd in run_dirs:
        try:
            candidates.extend(
                p for p in rd.glob("*.json")
                if not p.name.endswith((".envelope.json", ".goldfish.json"))
            )
        except OSError:
            continue
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    for rp in candidates:
        data: dict = {}
        try:
            raw = json.loads(rp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            pass
        sc: dict | None = None
        try:
            from henke.run_state import load_anchor_sidecar
            sc = load_anchor_sidecar(rp.stem, repo_root=repo_root)
        except Exception:
            sc = None
        sc_fields = sc.get("fields") if isinstance(sc, dict) and isinstance(sc.get("fields"), dict) else {}
        sp = (sc.get("spec_path") if isinstance(sc, dict) else None) or sc_fields.get("spec_path") or data.get("spec_path")
        if not _matches_spec(sp):
            continue
        spec_sha = (sc.get("spec_sha256") if isinstance(sc, dict) else None) or data.get("spec_sha256") or None
        head_start = (sc.get("head_start") if isinstance(sc, dict) else None) or data.get("head_start") or None
        return spec_sha, head_start

    # Defesa contra deleção do run file: inspeciona sidecars locais fora do repo.
    try:
        import hashlib
        from henke.rs_io import _config_dir
        from henke.run_state import load_anchor_sidecar
        tag = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
        anchors_d = _config_dir() / "anchors"
        if anchors_d.is_dir():
            sidecars = sorted(
                anchors_d.glob(f"{tag}-*.json"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                reverse=True,
            )
            for ap in sidecars:
                run_id = ap.stem[len(tag) + 1:]
                sc = load_anchor_sidecar(run_id, repo_root=repo_root)
                if not isinstance(sc, dict):
                    continue
                sc_fields = sc.get("fields") if isinstance(sc.get("fields"), dict) else {}
                sp = sc.get("spec_path") or sc_fields.get("spec_path")
                if _matches_spec(sp):
                    return sc.get("spec_sha256") or None, sc.get("head_start") or None
    except Exception:
        pass

    # Sem run ativa para esta spec: modo standalone (`henke check --spec`)
    fallback_sha: str | None = None
    if spec_abs.is_file():
        try:
            try:
                from henke.spec_drift import compute_spec_hash
            except ImportError:
                from spec_drift import compute_spec_hash  # type: ignore[no-redef]
            fallback_sha = compute_spec_hash(spec_abs)
        except Exception:
            pass
    fallback_head: str | None = None
    has_valid_baseline = False
    bp = repo_root / ".henke" / "gate-baseline.json"
    if bp.is_file():
        try:
            bdata = json.loads(bp.read_text(encoding="utf-8"))
            if isinstance(bdata, dict) and (bdata.get("head_commit") or bdata.get("head")):
                has_valid_baseline = True
        except (OSError, ValueError):
            pass
    if not has_valid_baseline and (repo_root / ".git").exists():
        fallback_head = "HEAD"
    return fallback_sha, fallback_head


def _drop_repo_syspath(repo_root: Path) -> None:
    """PT-6: PYTHONPATH/sys.path apontando para dentro do repo auditado fazia
    `import henke.metricas.*` no PROCESSO PAI carregar código hostil
    (sitecustomize/meta-path finder) antes de qualquer sandbox. Remove toda
    entrada que resolve dentro do alvo."""
    try:
        rr = Path(repo_root).resolve()
    except OSError:
        return
    kept = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            continue
        if resolved == rr or rr in resolved.parents:
            continue
        kept.append(entry)
    sys.path[:] = kept


def _cmd_check(
    repo_root: Path,
    spec_path: str | None = None,
    as_json: bool = False,
    no_cache: bool = False,
    clear_cache_flag: bool = False,
    staged: bool = False,
    full: bool = False,
    jobs: int = 1,
    gate_timeout: float = 120.0,
    expected_hash: str | None = None,
) -> int:
    _drop_repo_syspath(repo_root)
    if clear_cache_flag:
        clear_cache(repo_root)
        print("[OK] Cache de portões limpo.")

    import tempfile
    staged_temp_ctx = None
    target_root = repo_root

    if staged:
        # --staged implica --no-cache --full: manifestos, watch e cache desligados.
        no_cache = True
        full = True
        try:
            from henke.state_paths import sanitized_child_env
            _git_env = sanitized_child_env(repo_root)
            git_check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                env=_git_env,
            )
            if git_check.returncode == 0 and git_check.stdout.strip() == "true":
                # Merge-Audit 3: checkout-index roda filter.*.smudge de .git/config
                # (RCE no pre-commit). write-tree + archive não passam pelo
                # machinery de filtros de conteúdo.
                staged_temp_ctx = tempfile.TemporaryDirectory()
                # Banca-A 6: write-tree escreveria objetos novos em
                # .git/objects do repo auditado — redireciona para tempdir
                # com os objetos existentes como alternates.
                obj_dir = Path(staged_temp_ctx.name) / "objects"
                obj_dir.mkdir()
                _wt_env = dict(_git_env)
                _wt_env["GIT_OBJECT_DIRECTORY"] = str(obj_dir)
                # Delta-F16: `.git` pode ser arquivo (linked worktree/submodule)
                # — o dir real de objetos vem do git, não do path presumido.
                _gdir = subprocess.run(
                    ["git", "rev-parse", "--git-common-dir"],
                    cwd=str(repo_root), capture_output=True, text=True,
                    env=_git_env)
                if _gdir.returncode == 0 and _gdir.stdout.strip():
                    _gdir_p = Path(_gdir.stdout.strip())
                    if not _gdir_p.is_absolute():
                        _gdir_p = (repo_root / _gdir_p).resolve()
                    _objects = (_gdir_p / "objects").resolve()
                else:
                    _objects = (repo_root / ".git" / "objects").resolve()
                _wt_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(_objects)
                wt_res = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false",
                     "write-tree"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    env=_wt_env,
                )
                co_res = wt_res
                if wt_res.returncode == 0:
                    tree = wt_res.stdout.strip()
                    archive_path = Path(staged_temp_ctx.name) / "index.zip"
                    ar_res = subprocess.run(
                        ["git", "archive", "--format=zip",
                         "-o", str(archive_path), tree],
                        cwd=str(repo_root),
                        capture_output=True,
                        text=True,
                        # Delta-F1: archive precisa enxergar os objetos
                        # escritos no tempdir pelo write-tree — mesmo env.
                        env=_wt_env,
                    )
                    if ar_res.returncode == 0:
                        import zipfile
                        extract_dir = Path(staged_temp_ctx.name) / "tree"
                        extract_dir.mkdir()
                        extracted: set[str] = set()
                        with zipfile.ZipFile(archive_path) as zf:
                            for info in zf.infolist():
                                rel = info.filename.lstrip("/")
                                if ".." in rel.split("/"):
                                    continue  # path traversal não extraído
                                zf.extract(info, extract_dir)
                                extracted.add(rel)
                        archive_path.unlink()
                        # Delta-F2: `export-ignore`/`export-subst` de
                        # .gitattributes omitiriam/reescreveriam arquivos
                        # staged — o conjunto extraído TEM que bater com o
                        # ls-tree do índice (fail-closed em divergência).
                        lt_res = subprocess.run(
                            ["git", "ls-tree", "-rz", "--name-only", tree],
                            cwd=str(repo_root), capture_output=True,
                            text=True, env=_wt_env)
                        listed = (set(p for p in lt_res.stdout.split("\0") if p)
                                  if lt_res.returncode == 0 else set())
                        missing = sorted(p for p in listed
                                         if p + "/" not in extracted
                                         and p not in extracted)
                        if lt_res.returncode != 0 or missing:
                            print("ERRO: snapshot do índice diverge de "
                                  "ls-tree (export-ignore/export-subst em "
                                  f".gitattributes? omitidos={missing[:5]})",
                                  file=sys.stderr)
                            return 2
                        target_root = extract_dir
                    else:
                        co_res = ar_res
                if co_res.returncode == 0 and target_root == repo_root:
                    print("ERRO: Falha ao extrair snapshot do index Git via git archive.", file=sys.stderr)
                    return 2
                if co_res.returncode != 0:
                    print(f"ERRO: Falha ao extrair snapshot do index Git (exit {co_res.returncode}): {co_res.stderr}", file=sys.stderr)
                    return 2
            else:
                print("ERRO: --staged exige repositório Git.", file=sys.stderr)
                return 2
        except Exception as exc:
            print(f"ERRO: Falha ao extrair index Git para --staged: {exc}", file=sys.stderr)
            return 2

    try:
        checks_to_run: list[tuple[str, Path, list[str]]] = []
        manifest_scanners: list[str] = []

        # 1. Universal baseline gate: hazard_scan
        checks_to_run.append(("hazard_scan", GATE_MAP["hazard"], [str(target_root)]))
        manifest_scanners.append("hazard")

        # 2. Universal assertless scan: PT-7 — rodava só se `tests/`
        # existia; test_*.py na raiz ou em spec/ passavam sem auditoria.
        # O scanner decide elegibilidade (TEST_FILE) e sai n/a quando vazio.
        checks_to_run.append(("assertless_scan", GATE_MAP["assertless"], [str(target_root)]))
        manifest_scanners.append("assertless")

        # 3. Universal a11y scan: executado incondicionalmente para evitar regressões
        checks_to_run.append(("a11y_scan", GATE_MAP["a11y"], [str(target_root)]))
        manifest_scanners.append("a11y")

        if spec_path:
            checks_to_run.append((
                "contract_closure",
                GATE_MAP["contracts"],
                ["--repo", str(target_root), "--spec", spec_path],
            ))
            # PT-5: `--spec` sem spec_drift aprovava mutação fora do escopo —
            # o drift é o portão que compara spec contra o diff real.
            # O hash esperado vem de --expected-hash ou da âncora da run que
            # declarou esta spec; sem âncora o portão reprova (fail-closed).
            anchor_sha, anchor_head = _resolve_spec_anchor(repo_root, spec_path)
            eff_hash = expected_hash or anchor_sha
            # Em --staged, a spec validada também deve vir do snapshot do
            # índice, não do working tree com edições ainda não preparadas.
            staged_spec = (target_root / spec_path) if staged and not Path(spec_path).is_absolute() else Path(spec_path)
            if staged and Path(spec_path).is_absolute():
                try:
                    staged_spec = target_root / Path(spec_path).relative_to(repo_root)
                except ValueError:
                    print("ERRO: --spec absoluto fora do repositório em --staged.",
                          file=sys.stderr)
                    return 2
            drift_args = ["--repo", str(repo_root), "--spec", str(staged_spec)]
            if staged:
                drift_args.append("--staged-index")
            if eff_hash:
                drift_args += ["--expected-hash", eff_hash]
            if anchor_head:
                drift_args += ["--against", anchor_head]
            checks_to_run.append((
                "spec_drift",
                GATE_MAP["drift"],
                drift_args,
            ))

        use_manifests = not staged
        effective_no_cache = no_cache or staged
        results: list[dict] = []
        overall_ok = True

        if use_manifests:
            from henke.metricas.scan_manifest import (
                run_incremental_scan, scanner_specs,
            )
            # PT-6: um meta-path finder plantado (sitecustomize via
            # PYTHONPATH do repo) pode ter interceptado o import mesmo com
            # o sys.path podado — exige que o módulo venha da suíte.
            import henke.metricas.scan_manifest as _sm_mod
            try:
                _sm_file = Path(_sm_mod.__file__ or "").resolve()
                # Wheel/pipx: o módulo vive em <prefix>/site-packages/henke/...
                # enquanto SUITE_ROOT aponta p/ <prefix>/henke_data (só as
                # skills) — o container do pacote instalado também é raiz
                # confiável. Em checkout, ambos coincidem com a raiz do repo.
                _trusted = (Path(SUITE_ROOT).resolve(),
                            SCRIPTS_DIR.parent.resolve())
                if not any(_sm_file.is_relative_to(_t) for _t in _trusted):
                    print("ERRO: henke.metricas.scan_manifest resolveu "
                          "fora da suíte — possível sys.path poisoning.",
                          file=sys.stderr)
                    return 2
            except (OSError, TypeError):
                return 2
            spec_by_id = {s.scanner_id: s for s in scanner_specs()}
            manifest_results: dict[str, dict] = {}
            for name, _script, _args in checks_to_run:
                scanner_id = name.replace("_scan", "")
                if scanner_id not in manifest_scanners:
                    continue
                res = run_incremental_scan(
                    repo_root, spec_by_id[scanner_id],
                    full=full or effective_no_cache,
                )
                # DS-4: exit 3 = n/a (nada elegível a verificar) é veredito
                # físico honesto — não reprova, mas também não é PASS.
                passed = res.exit_code in (0, 3)
                if not passed:
                    overall_ok = False
                # P1-03: espelha o resultado incremental no event store v2.
                _mirror_event(repo_root, "scan_result", {
                    "scanner": scanner_id,
                    "exit_code": res.exit_code,
                    "full_scan": res.full_scan,
                    "scanned": list(res.scanned_files)[:200],
                })
                manifest_results[name] = {
                    "gate": name,
                    "exit_code": res.exit_code,
                    "passed": passed,
                    "output": _format_manifest_output(scanner_id, res),
                    "cached": False,
                    "manifest": {
                        "full_scan": res.full_scan,
                        "scanned": list(res.scanned_files),
                        "reused": len(res.reused_files),
                        "invalidated": len(res.invalidated_files),
                        "reasons": list(res.reasons),
                    },
                }
            results.extend(manifest_results[n] for n, *_ in checks_to_run
                           if n in manifest_results)

        # Portões sem adapter (ou staged) executam como processos supervisionados.
        script_gates = [(n, p, a) for n, p, a in checks_to_run
                        if n.replace("_scan", "") not in manifest_scanners
                        or not use_manifests]
        if script_gates:
            from henke.gate_parallel import (
                GateSpec, run_gates_parallel,
            )
            uncached: list[tuple[int, str, Path, list[str]]] = []
            cached_results: dict[int, dict] = {}
            for idx, (name, script_path, gate_args) in enumerate(script_gates):
                key = compute_cache_key(name, script_path, gate_args, repo_root)
                cached = None if effective_no_cache else get_cached_gate(key, repo_root=repo_root)
                if cached is not None:
                    exit_code = cached.get("exit_code", cached.get("returncode", 0))
                    passed = exit_code in (0, 3)
                    if not passed:
                        overall_ok = False
                    cached_results[idx] = {
                        "gate": name,
                        "exit_code": exit_code,
                        "passed": passed,
                        "output": cached.get("output", cached.get("stdout", "")).strip(),
                        "cached": True,
                    }
                else:
                    uncached.append((idx, name, script_path, gate_args))
            specs = [
                GateSpec(name, (sys.executable, "-Es", str(script_path), *gate_args),
                         target_root, timeout_sec=float(gate_timeout))
                for _idx, name, script_path, gate_args in uncached
            ]
            par_results = run_gates_parallel(
                specs, max_parallel=jobs, filter_root=repo_root)
            for (idx, name, script_path, gate_args), gres in zip(uncached, par_results):
                passed = gres.passed
                if not passed:
                    overall_ok = False
                out_text = ((gres.stdout or "") + (gres.stderr or "")).strip()
                if not staged:
                    store_cached_gate(
                        compute_cache_key(name, script_path, gate_args, repo_root),
                        {
                            "gate": name,
                            "exit_code": gres.exit_code,
                            "returncode": gres.exit_code,
                            "stdout": gres.stdout or "",
                            "stderr": gres.stderr or "",
                            "passed": passed,
                            "output": out_text,
                        },
                        repo_root=repo_root,
                    )
                    enforce_ram_limit(500.0, repo_root=repo_root)
                    # P1-03: espelha veredito no event store v2 (best-effort).
                    _mirror_event(repo_root, "gate_result", {
                        "key": compute_cache_key(
                            name, script_path, gate_args, repo_root),
                        "gate": name, "exit_code": gres.exit_code,
                        "passed": passed})
                cached_results[idx] = {
                    "gate": name,
                    "exit_code": gres.exit_code,
                    "passed": passed,
                    "output": out_text,
                    "cached": False,
                }
            for idx, (name, _p, _a) in enumerate(script_gates):
                results.append(cached_results[idx])

        if as_json:
            print(json.dumps({
                "repo": str(repo_root),
                "passed": overall_ok,
                "staged": staged,
                "gates": results,
            }, ensure_ascii=False, indent=2))
        else:
            for item in results:
                if item["exit_code"] == 3:
                    badge = "N/A (CACHED)" if item.get("cached") else "N/A"
                elif item.get("cached"):
                    badge = "CACHE HIT" if item["passed"] else "FAIL (CACHED)"
                else:
                    badge = "PASS" if item["passed"] else "FAIL"
                print(f"[{badge}] {item['gate']} (exit {item['exit_code']})")
                if item["output"]:
                    for ln in item["output"].splitlines():
                        print(f"  {ln}")

        if not staged:
            enforce_ram_limit(500.0, repo_root=repo_root)
        return 0 if overall_ok else 1
    finally:
        if staged_temp_ctx is not None:
            try:
                staged_temp_ctx.cleanup()
            except Exception:
                pass
