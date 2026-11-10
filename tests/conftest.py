"""conftest.py — isolamento da suíte de testes.

DS-5: a chave HMAC do gate cache / scan-manifest mora no perfil do usuário
(fora da árvore auditada). Testes não podem escrever chaves no perfil real
— redireciona HENKE_SECRET_DIR para um tempdir de sessão.
"""
import os
import tempfile

os.environ.setdefault(
    "HENKE_SECRET_DIR",
    tempfile.mkdtemp(prefix="henke-test-secrets-"),
)
