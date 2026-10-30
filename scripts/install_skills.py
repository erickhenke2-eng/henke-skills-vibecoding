#!/usr/bin/env python3
"""Install validated files with staging, rollback and manifest-based zombie cleanup."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile

MANIFEST = ".henke-manifest.json"
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def inventory(repo):
    """Full package: PROTOCOLO.md plus every file inside each henke-* skill dir.

    Subfolders (references/, agents/, scripts/) are shipped as-is; cache and
    editor debris are excluded so they never reach the user's machine.
    """
    files = [Path("PROTOCOLO.md")]
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
    return files


def read_manifest(path):
    """Previous install inventory; empty set when absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["files"]
        assert isinstance(entries, list) and all(isinstance(e, str) for e in entries)
        return set(entries)
    except (OSError, ValueError, KeyError, AssertionError):
        if path.exists():
            print(f"Aviso: manifesto ilegível em {path}; zumbis não serão removidos.", file=sys.stderr)
        return set()


def safe_member(target, rel):
    """Resolve a manifest entry inside target; reject traversal and symlinks."""
    rel_path = PurePosixPath(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts or rel == MANIFEST:
        raise ValueError(f"Entrada de manifesto insegura: {rel}")
    victim = target.joinpath(*rel_path.parts)
    cursor = target
    for part in rel_path.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Caminho de manifesto atravessa link simbólico: {rel}")
    return victim


def install(repo, user_root, dry_run=False):
    subprocess.run([sys.executable, str(repo / "scripts/validate_skills.py")], check=True)
    files = inventory(repo)
    targets = [user_root / p for p in (".agents/skills", ".gemini/config/skills", ".gemini/skills", ".claude/skills")]
    if dry_run:
        print("[DRY-RUN] " + "\n".join(map(str, targets)))
        return
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
            stale = sorted(read_manifest(target / MANIFEST) - new_set)
            victims = []
            for rel in stale:
                victim = safe_member(target, rel)
                if victim.is_symlink():
                    print(f"Aviso: zumbi ignorado por ser link simbólico: {victim}", file=sys.stderr)
                elif victim.is_file():
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
                shutil.copy2(repo / relative, staged)
                if hashlib.sha256(staged.read_bytes()).digest() != hashlib.sha256((repo / relative).read_bytes()).digest():
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
            for relative in files:
                destination = target.joinpath(*relative.parts)
                mkdir(destination.parent)
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
            manifest_new = {"version": 1, "files": sorted(new_set)}
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
    print(f"Instalados {len(files) - 1} arquivos de skills e PROTOCOLO.md nos quatro destinos.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-root", type=Path, default=Path.home())
    args = parser.parse_args()
    try:
        install(Path(__file__).resolve().parents[1], args.user_root.resolve(), args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Instalação falhou: {error}", file=sys.stderr)
        sys.exit(1)
