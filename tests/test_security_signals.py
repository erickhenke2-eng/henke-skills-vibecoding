#!/usr/bin/env python3
"""P1-04: fonte única de sinais — identidade router≡auto + corpus por sinal."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import henke_auto  # noqa: E402
import henke_router  # noqa: E402
import security_signals  # noqa: E402


class TestSingleSourceOfTruth(unittest.TestCase):
    # NB: import dual (scripts.X vs X) cria dois objetos-módulo iguais —
    # provamos identidade semântica (igualdade + __module__), não `is`.
    def test_router_uses_shared_module(self):
        self.assertEqual(tuple(henke_router.DESTRUCTIVE_SIGNALS),
                         tuple(security_signals.DESTRUCTIVE_SIGNALS))
        self.assertEqual(tuple(henke_router.PUBLISH_ONLY_SIGNALS),
                         tuple(security_signals.PUBLISH_ONLY_SIGNALS))
        self.assertTrue(henke_router._has_signal.__module__
                        .endswith("security_signals"))
        self.assertTrue(henke_router._norm.__module__
                        .endswith("security_signals"))
        self.assertTrue(henke_router._has_flex_signal.__module__
                        .endswith("security_signals"))

    def test_auto_uses_shared_module(self):
        self.assertEqual(tuple(henke_auto.DESTRUCTIVE_VERBS),
                         tuple(security_signals.DESTRUCTIVE_SIGNALS))
        self.assertEqual(tuple(henke_auto.PUBLISH_VERBS),
                         tuple(security_signals.PUBLISH_ONLY_SIGNALS))
        self.assertEqual(tuple(henke_auto.METACONFIG_SIGNALS),
                         tuple(security_signals.METACONFIG_SIGNALS))
        self.assertTrue(henke_auto._has_signal.__module__
                        .endswith("security_signals"))
        self.assertTrue(henke_auto._norm.__module__
                        .endswith("security_signals"))

    def test_no_divergence(self):
        """Antes do fix: 15 destrutivos só no auto, ~80 só no router."""
        self.assertEqual(set(henke_router.DESTRUCTIVE_SIGNALS),
                         set(henke_auto.DESTRUCTIVE_VERBS))
        self.assertEqual(set(henke_router.PUBLISH_ONLY_SIGNALS),
                         set(henke_auto.PUBLISH_VERBS))

    def test_formerly_auto_only_destructive_now_route(self):
        """Sinais coloquiais do auto agora também disparam auth no router."""
        for sig in ("get rid of", "joga fora", "tira do ar", "eliminate"):
            self.assertIn(sig, security_signals.DESTRUCTIVE_SIGNALS)


class TestSignalCorpus(unittest.TestCase):
    """Cada sinal precisa casar via _has_signal(_norm(sig)) — um sinal que
    não casa consigo mesmo é código morto/evasão gratuita."""

    def _assert_self_match(self, sigs, label):
        dead = []
        for sig in sigs:
            norm = security_signals._norm(sig)
            if not security_signals._has_signal(norm, sig):
                dead.append(sig)
        self.assertEqual(dead, [], f"{label}: sinais que não casam: {dead}")

    def test_destructive_corpus(self):
        self._assert_self_match(security_signals.DESTRUCTIVE_SIGNALS,
                                "destructive")

    def test_publish_corpus(self):
        self._assert_self_match(security_signals.PUBLISH_ONLY_SIGNALS,
                                "publish")

    def test_metaconfig_corpus(self):
        self._assert_self_match(security_signals.METACONFIG_SIGNALS,
                                "metaconfig")

    def test_canonical_prompts(self):
        cases = [
            ("deleta tudo agora", True), ("git push --force", True),
            ("dеlеta tudo", True),  # cirílico
            ("por favor explica o código", False),
        ]
        for prompt, expected in cases:
            norm = security_signals._norm(prompt)
            hit = any(security_signals._has_signal(norm, s)
                      for s in security_signals.DESTRUCTIVE_SIGNALS)
            self.assertEqual(hit, expected, prompt)


if __name__ == "__main__":
    unittest.main()
