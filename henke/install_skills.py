#!/usr/bin/env python3
"""Install validated files with staging, rollback and manifest-based zombie cleanup."""
import argparse
import contextlib
import hashlib
import json
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time

MANIFEST = ".henke-manifest.json"
INSTALL_LOCK = ".henke-install.lock"
LOCK_TIMEOUT_SEC = 120.0

# Teto por arquivo (banca v9 — M5): um blob gigante no inventário ia para
# 4 destinos com 3+ leituras completas (copy + verify + manifest hash).
MAX_FILE_SIZE = 64 * 1024 * 1024
LOCK_RETRY_SEC = 0.1
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


@contextlib.contextmanager
def _install_lock(user_root, timeout=LOCK_TIMEOUT_SEC):
    """Lock exclusivo por destino cobrindo stage→publish→manifest.

    Sem o lock, dois instaladores no mesmo user_root liam o manifesto de uma
    versão enquanto o outro publicava outra — misturando artefatos. O arquivo
    `.lock` nasce com O_CREAT|O_EXCL (criação atômica em POSIX e Windows) e
    carrega o PID para diagnóstico de lock estale.
    """
    user_root.mkdir(parents=True, exist_ok=True)
    lock_path = user_root / INSTALL_LOCK
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Lock de instalação retido por outro processo: "
                    f"{lock_path}. Se o instalador anterior morreu, remova o "
                    "arquivo e tente novamente.")
            time.sleep(LOCK_RETRY_SEC)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        yield
    finally:
        # A8: unlink só se o arquivo ainda é O NOSSO lock — se o lock expirou
        # e outro processo recriou o arquivo, remover apagaria o lock dele.
        try:
            same = os.stat(lock_path).st_ino == os.fstat(fd).st_ino
        except OSError:
            same = False
        os.close(fd)
        if same:
            try:
                lock_path.unlink()
            except OSError:
                pass


def _resolve_source(repo, rel):
    """Resolve o caminho físico de um arquivo do inventário.

    Num checkout é `repo/rel`; numa instalação via wheel, `repo` aponta para
    `<prefix>/henke_data` (skills e docs) enquanto `henke/*` vivem no pacote
    em site-packages — o fallback cobre esse layout.
    """
    src = repo / rel
    if src.is_file():
        return src
    pkg_root = Path(__file__).resolve().parents[1]
    alt = pkg_root / rel
    if alt.is_file():
        return alt
    return src


def inventory(repo):
    """Full package: PROTOCOLO.md plus every file inside each henke-* skill dir.

    Subfolders (references/, agents/, henke/) are shipped as-is; cache and
    editor debris are excluded so they never reach the user's machine.
    """
    files = [Path("PROTOCOLO.md")]
    if (repo / "docs" / "designs" / "TEMPLATE.md").is_file():
        files.append(Path("docs/designs/TEMPLATE.md"))
    for skill_dir in sorted(repo.glob("henke-*")):
        # Symlinks do repo-fonte não são seguidos — clone hostil com
        # `henke-x/leak -> ~/.ssh/config` instalaria conteúdo arbitrário na
        # área de skills (banca v7 — F-10).
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            continue
        repo_resolved = repo.resolve()
        for path in sorted(skill_dir.rglob("*")):
            # rglob pode descer em diretório-symlink (py<3.13): o resolved
            # tem que continuar dentro do repo — senão é link para fora.
            if path.is_symlink() or not path.is_file():
                continue
            try:
                if repo_resolved not in path.resolve().parents:
                    continue
            except OSError:
                continue
            rel = path.relative_to(repo)
            if any(part in IGNORED_DIRS for part in rel.parts):
                continue
            if path.name in IGNORED_FILES or path.suffix in IGNORED_SUFFIXES:
                continue
            files.append(rel)
    # [HENKE-PATCH] portões mecânicos e métricos instalados junto às skills que os invocam.
    # DS-7b: os scripts são fonte do PRÓPRIO pacote (__file__), não `repo/scripts`
    # — num wheel `repo` é henke_data/ e não tem henke/, então o instalador
    # distribuído instalava wrappers sem nenhum script funcional.
    _scripts_dir = Path(__file__).resolve().parent
    # Whitelist anterior apodreceu: state_paths/winpath/gate_cache/etc.
    # ficaram de fora e o `henke` instalado morria no primeiro subprocess.
    # Glob completo — a fonte é a própria suíte e a contenção abaixo já
    # valida cada entrada; módulo novo nunca mais fica de fora.
    for rel in sorted(_scripts_dir.glob("*.py")):
        if rel.is_file():
            files.append(Path("henke") / rel.name)
    for rel in sorted(_scripts_dir.glob("metricas/*.py")):
        if rel.is_file():
            files.append(Path("henke") / "metricas" / rel.name)
    # Wrappers da CLI global `henke`: instalados em <dest>/bin/ e resolvem
    # <dest>/henke/henke_cli.py automaticamente (caminho relativo ao bin/).
    for wrapper in (repo / "bin" / "henke", repo / "bin" / "henke.cmd"):
        if wrapper.is_file():
            files.append(wrapper.relative_to(repo))
    # Contenção unificada (banca v9 — A9): TODA entrada do inventário — skill,
    # script, bin/, arquivo raiz — resolve dentro do repo sem atravessar
    # symlink/junction. resolve() segue junction do Windows (is_symlink() não
    # pega junction); um `henke/metricas -> C:\evil` instalaria portões
    # arbitrários que a próxima run executa.
    repo_resolved = repo.resolve()
    # PT-03: num wheel `repo` é henke_data/ e `henke/*` vivem no pacote —
    # a contenção tem que aceitar as DUAS raízes legítimas e medir o arquivo
    # pela fonte real (_resolve_source), senão stat() num path inexistente
    # descartava TODO script em silêncio.
    pkg_resolved = Path(__file__).resolve().parents[1]
    confined: list[Path] = []
    for rel in files:
        src = _resolve_source(repo, rel)
        try:
            resolved = src.resolve()
        except OSError:
            continue
        under_repo = (resolved == repo_resolved
                      or repo_resolved in resolved.parents)
        under_pkg = (resolved == pkg_resolved
                     or pkg_resolved in resolved.parents)
        if not (under_repo or under_pkg):
            continue
        if src.is_symlink():
            continue
        try:
            if src.stat().st_size > MAX_FILE_SIZE:
                print(f"Aviso: arquivo acima de {MAX_FILE_SIZE // (1024 * 1024)}MB ignorado: {rel}", file=sys.stderr)
                continue
        except OSError:
            continue
        confined.append(rel)
    # PT-12: a ponte do contrato exige que módulos críticos de runtime
    # cheguem ao destino — falha ALTA e explícita se a contenção ou o glob
    # perder algum (nunca mais drop silencioso).
    _required = (
        "henke_router.py", "henke_cli.py", "state_paths.py", "winpath.py",
        "gate_cache.py", "gate_parallel.py", "process_runner.py",
        "process_supervisor.py", "test_impact.py", "mission_classifier.py",
        "henke_clean.py", "import_graph.py", "henke_watch.py",
        "henke_session.py", "ephemeral.py", "report_generator.py",
        "install_hook.py", "spec_drift.py", "repo_signals.py",
        "audit_ledger.py", "henke_gate.py", "run_state.py", "henke_auto.py",
        "henke_detect.py", "henke_gates.py", "henke_report.py",
        "henke_driver_agent.py", "_paths.py", "_isoboot.py", "gate_pipeline.py",
        "validate_skills.py",
        # Modulos do desmembramento (cli_*/rs_*/auto_*) — se o glob ou a
        # contencao perder um, henke_cli/henke_auto/run_state quebram no import.
        "cli_kernel.py", "cli_base.py", "cli_exec.py", "cli_exec_proc.py",
        "cli_check.py", "cli_runs.py", "cli_ops.py", "cli_pipeline.py",
        "rs_io.py", "rs_custody.py", "rs_git.py", "rs_verify.py",
        "rs_mutations.py",
        "auto_kernel.py", "auto_policy.py", "auto_driver.py",
        "auto_custody.py", "auto_audit.py", "auto_gates.py",
        "event_store.py", "security_signals.py", "exec_contract.py",
    )
    _shipped = {f.name for f in confined if f.parent == Path("henke")}
    _missing = [n for n in _required if n not in _shipped]
    if _missing:
        raise SystemExit(
            f"FALHA instalador: módulos obrigatórios fora do pacote: {_missing}")
    return confined


def read_manifest(path):
    """Previous install inventory and hashes; empty set and dict when absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("files", [])
        hashes = data.get("hashes", {})
        if not (isinstance(entries, list) and all(isinstance(e, str) for e in entries)):
            raise ValueError("manifesto: 'files' deve ser lista de strings")
        if not isinstance(hashes, dict):
            raise ValueError("manifesto: 'hashes' deve ser objeto")
        return set(entries), hashes
    except (OSError, ValueError, KeyError):
        if path.exists():
            print(f"Aviso: manifesto ilegível em {path}; zumbis não serão removidos.", file=sys.stderr)
        return set(), {}


def safe_member(target, rel):
    """Resolve a manifest entry inside target; reject traversal and symlinks."""
    normalized_rel = str(rel).replace("\\", "/")
    # B9 (banca v13): entrada "" → parts vazio → parts[-1] IndexError.
    if not normalized_rel.strip():
        raise ValueError(f"Entrada de manifesto vazia: {rel!r}")
    rel_path = PurePosixPath(normalized_rel)
    if rel_path.is_absolute() or ".." in rel_path.parts or rel == MANIFEST:
        raise ValueError(f"Entrada de manifesto insegura: {rel}")
    # Resolve só os PAIS — resolver o componente final transformava symlink
    # legítimo em seu alvo: ou abortava a instalação inteira, ou deletava o
    # arquivo resolvido deixando o link zumbi (banca v9 — M6).
    parent = target.joinpath(*rel_path.parts[:-1]).resolve()
    victim = parent / rel_path.parts[-1]
    target_resolved = target.resolve()
    if not victim.is_relative_to(target_resolved):
        raise ValueError(f"Caminho de manifesto escapa do destino: {rel}")
    cursor = target
    for part in rel_path.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Caminho de manifesto atravessa link simbólico: {rel}")
    return victim


def install(repo, user_root, dry_run=False, lock_timeout=LOCK_TIMEOUT_SEC):
    # A5 (banca v11): o validador da FONTE executava antes de qualquer
    # contenção — pacote hostil rodava código com privilégio do usuário no
    # install. Prefere a cópia da PRÓPRIA suíte (henke/ do pacote).
    _validator = Path(__file__).resolve().parent / "validate_skills.py"
    if not _validator.is_file():
        _validator = _resolve_source(repo, Path("henke/validate_skills.py"))
    # banca-v18 (rev D-F6): único filho da suíte sem timeout — validador
    # pendurado travava o instalador para sempre.
    subprocess.run([sys.executable, "-Es", str(_validator)],
                   check=True, timeout=300)
    files = inventory(repo)
    targets = [user_root / p for p in (".agents/skills", ".gemini/config/skills", ".gemini/skills", ".claude/skills")]
    if dry_run:
        print("[DRY-RUN] " + "\n".join(map(str, targets)))
        return
    # Lock exclusivo por destino: cobre TODA a transação stage→publish→manifesto
    # — instaladores paralelos não podem misturar versões dos artefatos.
    with _install_lock(user_root, timeout=lock_timeout):
        _publish(repo, targets, files)


def _publish(repo, targets, files):
    """Transação stage→publish→manifest (invocada sob _install_lock)."""
    # Stage all content before changing any installed file; keep unrelated skills intact.
    transactions, changed, created = [], [], []
    def mkdir(path):
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
    try:
        stale_map = {}
        for target in targets:
            if target.is_symlink():
                raise ValueError(f"Destino com link simbólico: {target}")
            mkdir(target)
            stage = Path(tempfile.mkdtemp(prefix=".henke-install-", dir=target))
            transactions.append(stage)
            new_set = {rel.as_posix() for rel in files}
            old_set, old_hashes = read_manifest(target / MANIFEST)
            stale = sorted(old_set - new_set)
            victims = []
            for rel in stale:
                victim = safe_member(target, rel)
                if victim.is_symlink():
                    print(f"Aviso: zumbi ignorado por ser link simbólico: {victim}", file=sys.stderr)
                elif victim.is_file():
                    # Só remove com prova de identidade via hash do manifesto v2.
                    rel_norm = str(rel).replace("\\", "/")
                    expected_hash = old_hashes.get(rel_norm)
                    if not expected_hash:
                        print(f"Aviso: arquivo legado preservado sem hash de origem confiável: {victim}", file=sys.stderr)
                        continue
                    current_hash = hashlib.sha256(victim.read_bytes()).hexdigest()
                    if current_hash != expected_hash:
                        print(f"Aviso: arquivo stale preservado por modificação do usuário: {victim}", file=sys.stderr)
                        continue
                    victims.append((rel, victim))
                elif victim.exists():
                    raise ValueError(f"Zumbi do manifesto não é arquivo: {victim}")
            stale_map[target] = victims
            for relative in files:
                destination = target.joinpath(*relative.parts)
                inside = destination.relative_to(target).parts[:-1]
                cursor = target
                for part in inside:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        raise ValueError(f"Destino com link simbólico: {cursor}")
                    if cursor.exists() and not cursor.is_dir():
                        raise FileExistsError(f"Caminho colide com arquivo existente (esperado diretório): {cursor}")
                if destination.is_symlink():
                    raise ValueError(f"Destino com link simbólico: {destination}")
                if destination.exists() and not destination.is_file():
                    raise ValueError(f"Destino não é arquivo: {destination}")
                staged = stage / "new" / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                # A6 (banca v11): re-checa no instante da cópia — entre o
                # inventário e o copy2 um processo concorrente pode trocar o
                # arquivo por symlink para fora do repo (vazava ~/.ssh p/ a
                # skill instalada). O re-check por path não basta: resolve
                # e confina às duas raízes legítimas (repo OU pacote).
                _src = _resolve_source(repo, relative)
                if _src.is_symlink():
                    raise ValueError(
                        f"Fonte virou link simbólico após o inventário: {relative}")
                _roots = (Path(repo).resolve(),
                          Path(__file__).resolve().parents[1].resolve())
                _sr = _src.resolve()
                if not any(_sr == r or r in _sr.parents for r in _roots):
                    raise ValueError(f"Fonte escapa do pacote: {relative}")
                shutil.copy2(_src, staged)
                if hashlib.sha256(staged.read_bytes()).digest() != hashlib.sha256(_src.read_bytes()).digest():
                    raise ValueError(f"Cópia divergente: {relative}")
                backup = stage / "old" / relative
                if destination.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
            for rel, victim in victims:
                backup = stage / "stale" / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(victim, backup)
            old_manifest = target / MANIFEST
            if old_manifest.exists() and old_manifest.is_file() and not old_manifest.is_symlink():
                (stage / "old").mkdir(exist_ok=True)
                shutil.copy2(old_manifest, stage / "old" / MANIFEST)
        for target, stage in zip(targets, transactions):
            target_resolved = target.resolve()
            for relative in files:
                destination = target.joinpath(*relative.parts)
                mkdir(destination.parent)
                # Rechecagem no instante da escrita (TOCTOU): entre a validação
                # do stage e o os.replace, um diretório pode virar symlink que
                # desviaria a escrita para fora do destino (ALTO-8 da banca).
                real_dest = destination.resolve()
                if real_dest != destination.absolute() and not real_dest.is_relative_to(target_resolved):
                    raise ValueError(f"Destino escapa por link simbólico: {destination}")
                parent_resolved = destination.parent.resolve()
                if not parent_resolved.is_relative_to(target_resolved):
                    raise ValueError(f"Diretório de destino atravessa link simbólico: {destination}")
                # Record before replacing so rollback also handles an interrupted replacement.
                changed.append((destination, stage / "old" / relative))
                os.replace(stage / "new" / relative, destination)
            for rel, victim in stale_map[target]:
                changed.append((victim, stage / "stale" / rel))
                victim.unlink()
                directory = victim.parent
                while directory != target:
                    try:
                        directory.rmdir()
                    except OSError:
                        break  # não vazio: contém arquivos do usuário ou de terceiros
                    directory = directory.parent
            manifest_new = {
                "version": 2,
                "files": sorted(new_set),
                "hashes": {
                    # Hash dos bytes INSTALADOS (destino), não da fonte re-lida —
                    # mutação entre cópia e manifesto criava zumbi permanente
                    # (banca v9 — M5).
                    str(rel).replace("\\", "/"): hashlib.sha256((target / rel).read_bytes()).hexdigest()
                    for rel in files
                }
            }
            staged_manifest = stage / "new" / MANIFEST
            staged_manifest.write_text(json.dumps(manifest_new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            changed.append((target / MANIFEST, stage / "old" / MANIFEST))
            os.replace(staged_manifest, target / MANIFEST)
    except BaseException:
        rollback_errors = []
        for destination, backup in reversed(changed):
            try:
                if backup.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, destination)
                else:
                    try:
                        if destination.is_file() or destination.is_symlink():
                            destination.unlink()
                    except OSError:
                        pass
            except OSError as error:
                rollback_errors.append(str(error))
        if rollback_errors:
            raise RuntimeError("Rollback incompleto; backups preservados em " + str(transactions) + ": " + str(rollback_errors))
        raise
    finally:
        # Preserve backups if recovery itself failed; never delete outside these staging paths.
        if not locals().get("rollback_errors"):
            for stage in transactions:
                shutil.rmtree(stage)
            for directory in reversed(created):
                try:
                    directory.rmdir()
                except OSError:
                    pass
    # Wrapper POSIX precisa de bit executável no destino (o bit pode se perder no
    # checkout em Windows/copy2); no Windows o .cmd já é invocável sem chmod.
    if os.name == "posix":
        for target in targets:
            sh_wrapper = target / "bin" / "henke"
            if sh_wrapper.is_file():
                try:
                    sh_wrapper.chmod(sh_wrapper.stat().st_mode | 0o755)
                except OSError:
                    pass
    print(f"Instalados {len(files) - 1} arquivos de skills e PROTOCOLO.md nos quatro destinos.")
    bin_dirs = [target / "bin" for target in targets if (target / "bin" / "henke").is_file()]
    if bin_dirs:
        print()
        print("CLI 'henke' instalada. Para chamá-la de qualquer diretório, adicione UM destes caminhos ao PATH:")
        for d in bin_dirs:
            print(f"  - {d}")
        print("Alternativa sem PATH: 'pipx install .' ou 'pip install .' na raiz deste repositório "
              "(expõe 'henke' via [project.scripts]).")


if __name__ == "__main__":
    # ISO-BOOT canônico (henke/_isoboot.py) — sitecustomize/usercustomize/
    # .pth hostis executam no startup e patcham verificadores no pai; o
    # respawn -Es nasce sem esses canais (prova ESTRUTURAL: sys.flags
    # autenticado + os.sys — nunca variável de ambiente). Texto sincronizado
    # por tests/test_isoboot_guard.py — editar só alterando o canônico.
    import importlib.util as _ilu
    import os.path as _osp
    _ib_p = _osp.join(_osp.dirname(_osp.abspath(__file__)), "_isoboot.py")
    if not _osp.isfile(_ib_p):
        _ib_p = _osp.join(_osp.dirname(_osp.dirname(_osp.abspath(__file__))),
                          "_isoboot.py")
    _ib_spec = _ilu.spec_from_file_location("henke_isoboot", _ib_p)
    _ib_mod = _ilu.module_from_spec(_ib_spec)
    _ib_spec.loader.exec_module(_ib_mod)
    _ib_mod.ensure_isolated(__file__, sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-root", type=Path, default=Path.home())
    args = parser.parse_args()
    try:
        try:
            from henke._paths import suite_root
        except ImportError:
            from _paths import suite_root
        install(suite_root(), args.user_root.resolve(), args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Instalação falhou: {error}", file=sys.stderr)
        sys.exit(1)
