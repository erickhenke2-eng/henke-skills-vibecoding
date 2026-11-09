#!/usr/bin/env python3
"""Install validated files with staging, rollback and manifest-based zombie cleanup."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time

MANIFEST = ".henke-manifest.json"
INSTALL_LOCK = ".henke-install.lock"
LOCK_TIMEOUT_SEC = 120.0
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
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def _resolve_source(repo, rel):
    """Resolve o caminho físico de um arquivo do inventário.

    Num checkout é `repo/rel`; numa instalação via wheel, `repo` aponta para
    `<prefix>/henke_data` (skills e docs) enquanto `scripts/*` vivem no pacote
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

    Subfolders (references/, agents/, scripts/) are shipped as-is; cache and
    editor debris are excluded so they never reach the user's machine.
    """
    files = [Path("PROTOCOLO.md")]
    if (repo / "docs" / "designs" / "TEMPLATE.md").is_file():
        files.append(Path("docs/designs/TEMPLATE.md"))
    for skill_dir in sorted(repo.glob("henke-*")):
        if not skill_dir.is_dir():
            continue
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(repo)
            if any(part in IGNORED_DIRS for part in rel.parts):
                continue
            if path.name in IGNORED_FILES or path.suffix in IGNORED_SUFFIXES:
                continue
            files.append(rel)
    # [HENKE-PATCH] portões mecânicos e métricos instalados junto às skills que os invocam
    root_gate_scripts = [
        repo / "scripts" / "_paths.py",          # importado por vários scripts — sem ele o CLI morre
        repo / "scripts" / "gate_pipeline.py",   # invocado por henke_gates (ratchet/fence)
        repo / "scripts" / "validate_skills.py", # chamado por install_skills e doctor
        repo / "scripts" / "repo_signals.py",
        repo / "scripts" / "henke_router.py",
        repo / "scripts" / "spec_drift.py",
        repo / "scripts" / "audit_ledger.py",
        repo / "scripts" / "henke_gate.py",
        repo / "scripts" / "henke_cli.py",
        repo / "scripts" / "run_state.py",
        repo / "scripts" / "henke_auto.py",
        repo / "scripts" / "henke_detect.py",
        repo / "scripts" / "henke_gates.py",
        repo / "scripts" / "henke_report.py",
        repo / "scripts" / "henke_driver_opencode.py",
        repo / "scripts" / "henke_driver_agent.py",
    ]
    for rel in sorted(repo.glob("scripts/metricas/*.py")) + root_gate_scripts:
        if rel.is_file():
            files.append(rel.relative_to(repo))
    # Wrappers da CLI global `henke`: instalados em <dest>/bin/ e resolvem
    # <dest>/scripts/henke_cli.py automaticamente (caminho relativo ao bin/).
    for wrapper in (repo / "bin" / "henke", repo / "bin" / "henke.cmd"):
        if wrapper.is_file():
            files.append(wrapper.relative_to(repo))
    return files


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
    rel_path = PurePosixPath(normalized_rel)
    if rel_path.is_absolute() or ".." in rel_path.parts or rel == MANIFEST:
        raise ValueError(f"Entrada de manifesto insegura: {rel}")
    victim = target.joinpath(*rel_path.parts).resolve()
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
    subprocess.run(
        [sys.executable, str(_resolve_source(repo, Path("scripts/validate_skills.py")))],
        check=True,
    )
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
                shutil.copy2(_resolve_source(repo, relative), staged)
                if hashlib.sha256(staged.read_bytes()).digest() != hashlib.sha256(_resolve_source(repo, relative).read_bytes()).digest():
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
                    str(rel).replace("\\", "/"): hashlib.sha256(_resolve_source(repo, rel).read_bytes()).hexdigest()
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-root", type=Path, default=Path.home())
    args = parser.parse_args()
    try:
        try:
            from scripts._paths import suite_root
        except ImportError:
            from _paths import suite_root
        install(suite_root(), args.user_root.resolve(), args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Instalação falhou: {error}", file=sys.stderr)
        sys.exit(1)
