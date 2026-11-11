#!/usr/bin/env python3
"""
Testes automatizados do validador (henke/validate_skills.py).
Comprova que o validador rejeita exatamente as 4 adulterações auditadas pelo GPT Astra
e aprova o repositório canônico legítimo.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parents[1]
validate_script = repo_root / "henke" / "validate_skills.py"


def run_validator(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(cwd / "henke" / "validate_skills.py")],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8"
    )


def copy_repo_to_temp() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)
    # Copia arquivos essenciais: PROTOCOLO.md, henke/ e henke-*/
    shutil.copy2(repo_root / "PROTOCOLO.md", tmp_path / "PROTOCOLO.md")
    shutil.copytree(repo_root / "henke", tmp_path / "henke")
    for d in repo_root.glob("henke-*"):
        if d.is_dir():
            shutil.copytree(d, tmp_path / d.name)
    return tmp


class TestValidator(unittest.TestCase):
    def test_legitimate_repo(self):
        print("Teste 1: Repositório canônico legítimo deve passar...")
        res = run_validator(repo_root)
        self.assertEqual(res.returncode, 0, f"Falha inesperada no repo legítimo: {res.stdout}\n{res.stderr}")
        self.assertIn("OK: 58 skills com estrutura válida", res.stdout)
        self.assertIn("Semântica e execução não verificadas", res.stdout)
        print("  [PASSOU] Repositório legítimo aprovado com exit code 0.")

    def test_astra_repro_1_gutted_protocol(self):
        print("Teste 2: Rejeição de PROTOCOLO adulterado com 101 letras 'x'...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            (tmp_path / "PROTOCOLO.md").write_text("x" * 101, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertNotEqual(res.returncode, 0, "Deveria ter falhado com PROTOCOLO.md adulterado!")
            self.assertTrue("sem estrutura canônica obrigatória" in res.stdout or "insuficiente" in res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_astra_repro_2_invalid_yaml_bracket(self):
        print("Teste 3: Rejeição de 'description: [' (YAML corrompido)...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
            content = target_skill.read_text(encoding="utf-8")
            content = re.sub(r"description:\s*.*", "description: [", content)
            target_skill.write_text(content, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertNotEqual(res.returncode, 0, "Deveria ter falhado com YAML corrompido!")
            self.assertIn("YAML inválido", res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_astra_repro_3_duplicate_name_key(self):
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
            self.assertNotEqual(res.returncode, 0, "Deveria ter falhado com chave duplicada no YAML!")
            self.assertIn("Chave duplicada detectada", res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_astra_repro_4_broken_relative_link(self):
        print("Teste 5: Rejeição de link relativo para arquivo inexistente ('../missing/PROTOCOLO.md')...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
            content = target_skill.read_text(encoding="utf-8")
            content = content.replace("[PROTOCOLO.md](../PROTOCOLO.md)", "[PROTOCOLO.md](../missing/PROTOCOLO.md)")
            target_skill.write_text(content, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertNotEqual(res.returncode, 0, "Deveria ter falhado com link relativo quebrado!")
            self.assertIn("link relativo quebrado", res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_f1_fake_link_inside_code_fence_passes(self):
        print("Teste 6: F1 — link fictício dentro de code fence/inline code NÃO deve falhar...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
            content = target_skill.read_text(encoding="utf-8")
            content += (
                "\n\nExemplo documental de sintaxe de link:\n\n"
                "```markdown\n"
                "Veja [documento](../nao-existe.md) e [outro](./fantasma/x.md).\n"
                "```\n\n"
                "Também inline: `[fake](../fantasma.md)` não é link real.\n"
            )
            target_skill.write_text(content, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertEqual(res.returncode, 0, f"F1 falhou: link em fence/inline foi tratado como real: {res.stdout}")
            print("  [PASSOU] Links dentro de código ignorados (exit code 0).")
        finally:
            tmp.cleanup()

    def test_f1_same_link_outside_fence_fails(self):
        print("Teste 7: F1 (controle) — o MESMO link fora do fence deve falhar...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
            content = target_skill.read_text(encoding="utf-8")
            content += "\n\nVeja [documento](../nao-existe.md) fora de qualquer código.\n"
            target_skill.write_text(content, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertNotEqual(res.returncode, 0, "Link quebrado fora de fence passou!")
            self.assertIn("link relativo quebrado", res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_f2_section6_without_protocol_link_fails(self):
        print("Teste 8: F2 — seção '## 6.' sem vínculo canônico ao PROTOCOLO deve falhar...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
            content = target_skill.read_text(encoding="utf-8")
            idx = content.index("## 6.")
            # Troca o vínculo canônico por link válido para outro arquivo existente.
            tail = content[idx:].replace(
                "[PROTOCOLO.md](../PROTOCOLO.md)",
                "[outra skill](../henke-arch-banco/SKILL.md)"
            )
            self.assertNotEqual(tail, content[idx:], "Vínculo canônico não encontrado na seção 6 para o teste")
            target_skill.write_text(content[:idx] + tail, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertNotEqual(res.returncode, 0, "Seção 6 sem link ao PROTOCOLO passou!")
            self.assertIn("sem link markdown resolvível para '../PROTOCOLO.md'", res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_f5_missing_canonical_section_fails(self):
        print("Teste 9: F5 — skill sem a seção operacional '## 3.' deve falhar...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            target_skill = tmp_path / "henke-blindagem" / "SKILL.md"
            content = target_skill.read_text(encoding="utf-8")
            content, count = re.subn(r"^## 3\.", "### 3.", content, count=1, flags=re.M)
            self.assertEqual(count, 1, "Seção '## 3.' não encontrada para adulterar")
            target_skill.write_text(content, encoding="utf-8")
            res = run_validator(tmp_path)
            self.assertNotEqual(res.returncode, 0, "Skill sem '## 3.' passou!")
            self.assertIn("seção operacional canônica '## 3.' ausente", res.stdout)
            print("  [PASSOU] Rejeitado com sucesso (exit code != 0).")
        finally:
            tmp.cleanup()

    def test_installer_aborts_on_invalid_package(self):
        print("Teste 10: Instalador aborta antes de escrever se o pacote for inválido...")
        tmp = copy_repo_to_temp()
        try:
            tmp_path = Path(tmp.name)
            # Remove PROTOCOLO.md
            (tmp_path / "PROTOCOLO.md").unlink()
            # Usa o instalador da plataforma e um destino temporário explícito.
            shutil.copy2(repo_root / "install.ps1", tmp_path / "install.ps1")
            shutil.copy2(repo_root / "install.sh", tmp_path / "install.sh")
            destination = tmp_path / "destination"
            if sys.platform == "win32":
                command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "install.ps1"), "-UserRoot", str(destination)]
            else:
                command = ["bash", str(tmp_path / "install.sh"), "--user-root", str(destination)]
            ps_res = subprocess.run(command, cwd=str(tmp_path), capture_output=True, text=True, errors="replace")
            self.assertFalse(destination.exists(), "Pacote inválido criou destinos!")
            self.assertNotEqual(ps_res.returncode, 0, "O instalador deveria ter abortado com exit code != 0!")
            print("  [PASSOU] Instalador abortou com sucesso antes de modificar destinos.")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
