"""Testes do diretório de estado Henke (`henke/state_paths.py`).

Contrato: `HENKE_STATE_DIR` absoluto redireciona `.henke`; relativo é erro de
uso explícito; ausente, o estado fica em `<repo>/.henke`.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from henke.state_paths import state_dir


class TestStateDir(unittest.TestCase):
    """Resolução de state_dir com e sem HENKE_STATE_DIR."""

    def test_default_is_repo_dot_henke(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HENKE_STATE_DIR", None)
                self.assertEqual(state_dir(repo), repo.resolve() / ".henke")

    def test_absolute_override_redirects(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ext:
            repo = Path(td)
            with patch.dict(os.environ, {"HENKE_STATE_DIR": ext}):
                self.assertEqual(state_dir(repo), Path(ext))

    def test_relative_override_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"HENKE_STATE_DIR": "relative/dir"}):
                with self.assertRaises(ValueError):
                    state_dir(Path(td))

    def test_override_does_not_silently_fall_back(self):
        """Override inválido nunca redireciona para dentro do repo."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"HENKE_STATE_DIR": ".henke"}):
                with self.assertRaises(ValueError):
                    state_dir(Path(td))


if __name__ == "__main__":
    unittest.main()
