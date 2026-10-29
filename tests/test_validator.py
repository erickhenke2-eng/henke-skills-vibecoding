#!/usr/bin/env python3
"""
Testes automatizados do validador (scripts/validate_skills.py).
Comprova que o validador rejeita exatamente as 4 adulterações auditadas pelo GPT Astra
e aprova o repositório canônico legítimo.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parents[1]
validate_script = repo_root / "scripts" / "validate_skills.py"

def run_validator(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "validate_skills.py")],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8"
    )

def copy_repo_to_temp() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)
    # Copia arquivos essenciais: PROTOCOLO.md, scripts/ e henke-*/
    shutil.copy2(repo_root / "PROTOCOLO.md", tmp_path / "PROTOCOLO.md")
    shutil.copytree(repo_root / "scripts", tmp_path / "scripts")
    for d in repo_root.glob("henke-*"):
        if d.is_dir():
            shutil.copytree(d, tmp_path / d.name)
    return tmp

def test_legitimate_repo():
    print("Teste 1: Repositório canônico legítimo deve passar...")
    res = run_validator(repo_root)
    assert res.returncode == 0, f"Falha inesperada no repo legítimo: {res.stdout}\n{res.stderr}"
    assert "OK: 49 skills válidas" in res.stdout
    print("  [PASSOU] Repositório legítimo aprovado com exit code 0.")

def test_astra_repro_1_gutted_protocol():
    print("Teste 2: Rejeição de PROTOCOLO adulterado com 101 letras 'x'...")
    tmp = copy_repo_to_temp()
    try:
        tmp_path = Path(tmp.name)
        (tmp_path / "PROTOCOLO.md").write_text("x" * 101, encoding="utf-8")
        res = run_validator(tmp_path)
        assert res.returncode != 0, "Deveria ter falhado com PROTOCOLO.md adulterado!"
        assert "sem estrutura canônica obrigatória" in res.stdout or "insuficiente" in res.stdout
        print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
    finally:
        tmp.cleanup()

def test_astra_repro_2_invalid_yaml_bracket():
    print("Teste 3: Rejeição de 'description: [' (YAML corrompido)...")
    tmp = copy_repo_to_temp()
    try:
        tmp_path = Path(tmp.name)
        target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
        content = target_skill.read_text(encoding="utf-8")
        content = re.sub(r"description:\s*.*", "description: [", content)
        target_skill.write_text(content, encoding="utf-8")
        res = run_validator(tmp_path)
        assert res.returncode != 0, "Deveria ter falhado com YAML corrompido!"
        assert "YAML inválido" in res.stdout
        print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
    finally:
        tmp.cleanup()

def test_astra_repro_3_duplicate_name_key():
    print("Teste 4: Rejeição de chave 'name:' duplicada e conflitante...")
    tmp = copy_repo_to_temp()
    try:
        tmp_path = Path(tmp.name)
        target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
        content = target_skill.read_text(encoding="utf-8")
        # Injeta uma segunda chave name
        content = content.replace("name: henke-blindagem", "name: henke-blindagem\nname: henke-conflito")
        target_skill.write_text(content, encoding="utf-8")
        res = run_validator(tmp_path)
        assert res.returncode != 0, "Deveria ter falhado com chave duplicada no YAML!"
        assert "Chave duplicada detectada" in res.stdout
        print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
    finally:
        tmp.cleanup()

def test_astra_repro_4_broken_relative_link():
    print("Teste 5: Rejeição de link relativo para arquivo inexistente ('../missing/PROTOCOLO.md')...")
    tmp = copy_repo_to_temp()
    try:
        tmp_path = Path(tmp.name)
        target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
        content = target_skill.read_text(encoding="utf-8")
        content = content.replace("[PROTOCOLO.md](../PROTOCOLO.md)", "[PROTOCOLO.md](../missing/PROTOCOLO.md)")
        target_skill.write_text(content, encoding="utf-8")
        res = run_validator(tmp_path)
        assert res.returncode != 0, "Deveria ter falhado com link relativo quebrado!"
        assert "link relativo quebrado" in res.stdout
        print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
    finally:
        tmp.cleanup()

def test_installer_aborts_on_invalid_package():
    print("Teste 6: Instalador aborta antes de escrever se o pacote for inválido...")
    tmp = copy_repo_to_temp()
    try:
        tmp_path = Path(tmp.name)
        # Remove PROTOCOLO.md
        (tmp_path / "PROTOCOLO.md").unlink()
        shutil.copy2(repo_root / "install.ps1", tmp_path / "install.ps1")
        # Roda install.ps1 com dry-run no repo corrompido
        ps_res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "install.ps1"), "-DryRun"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            errors="replace"
        )
        assert ps_res.returncode != 0, "O instalador deveria ter abortado com exit code != 0!"
        print("  [PASSOU] Instalador abortou com sucesso antes de modificar destinos.")
    finally:
        tmp.cleanup()

if __name__ == "__main__":
    test_legitimate_repo()
    test_astra_repro_1_gutted_protocol()
    test_astra_repro_2_invalid_yaml_bracket()
    test_astra_repro_3_duplicate_name_key()
    test_astra_repro_4_broken_relative_link()
    test_installer_aborts_on_invalid_package()
    print("\nTodos os 6 testes de validação e instalação passaram com perfeição!")
