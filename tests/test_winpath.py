r"""Testes de scripts/winpath.py (Wave 7 — R7).

Cobertura real (sem mocks circulares; toda asserção verifica resultado):
1. ``is_reserved_component`` bilateral (nul/CON.txt/aux /prn./COM1.log/lpt9
   versus null/console.txt/com10/normal.py).
2. ``canonical_path``: idempotência, ``\\.\``, relativo sem base, ``..``
   fora da contenção, Unicode, UNC→``\\?\UNC\`` e prefixo não duplicado
   (Win32), normalização POSIX.
3. ``assert_safe_relative``: aceita ``a/b/c.py``; rejeita absoluto, ``..``,
   componentes vazios e reservados.
4. ``is_within``: dentro/fora/irmão com prefixo comum/igual.
5. ``iter_tree``: ordem determinística, prune, max_depth, diretório
   desaparecido→error, raiz inválida, junction cíclica e junction para fora
   (Win32, mklink /J real), symlink POSIX.
6. ``sha256_file``: hash conhecido, arquivo instável (fault injection
   determinística em ``_lstat``), reparse rejeitado.
7. Caminho >260 chars no Windows criado de verdade via ``\\?\`` e hasheado.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts.winpath import (
    FileDigest,
    PathSafetyError,
    TreeEntry,
    UnstableFileError,
    assert_safe_relative,
    canonical_path,
    is_reserved_component,
    is_within,
    iter_tree,
    sha256_file,
)

IS_WIN = sys.platform == "win32"


class TempTreeTest(unittest.TestCase):
    """Base com TemporaryDirectory e cleanup garantido via addCleanup."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


# ---------------------------------------------------------------------------
# 1. Nomes reservados
# ---------------------------------------------------------------------------


class TestReservedComponents(unittest.TestCase):
    def test_reserved_names_are_rejected_for_write(self):
        for name in ("nul", "CON.txt", "aux ", "prn.", "COM1.log", "lpt9"):
            with self.subTest(name=name):
                self.assertTrue(is_reserved_component(name))
        for name in ("null", "console.txt", "com10", "normal.py"):
            with self.subTest(name=name):
                self.assertFalse(is_reserved_component(name))

    def test_reserved_case_and_trailing_noise(self):
        for name in ("NUL", "con", "Aux.TXT", "CoM5", "LPT9 ", "con "):
            with self.subTest(name=name):
                self.assertTrue(is_reserved_component(name))

    def test_reserved_non_reserved_edge_names(self):
        for name in ("com0", "com10", "lpt0", "lpt10", "nulll", "conx",
                     "prn2", "auxiliary", "a", "x.txt"):
            with self.subTest(name=name):
                self.assertFalse(is_reserved_component(name))


# ---------------------------------------------------------------------------
# 2. canonical_path
# ---------------------------------------------------------------------------


class TestCanonicalPath(TempTreeTest):
    def test_idempotent_absolute(self):
        once = canonical_path(self.root / "sub" / "f.txt")
        twice = canonical_path(once)
        self.assertEqual(once, twice)
        self.assertTrue(once.is_absolute())

    def test_relative_without_base_rejected(self):
        with self.assertRaises(PathSafetyError):
            canonical_path("relativo/arquivo.txt")

    def test_empty_and_nul_rejected(self):
        with self.assertRaises(PathSafetyError):
            canonical_path("")
        with self.assertRaises(PathSafetyError):
            canonical_path("abc\x00def")

    def test_device_namespace_rejected(self):
        for bad in ("\\\\.\\PhysicalDrive0", "\\\\.\\COM1", "//./nul"):
            with self.subTest(bad=bad):
                with self.assertRaises(PathSafetyError):
                    canonical_path(bad)

    def test_dotdot_escaping_base_rejected(self):
        with self.assertRaises(PathSafetyError):
            canonical_path("..\\fora" if IS_WIN else "../fora", base=self.root)
        with self.assertRaises(PathSafetyError):
            canonical_path("sub/../../fora", base=self.root)

    def test_dotdot_contained_is_normalized(self):
        got = canonical_path("sub/../f.txt", base=self.root)
        self.assertTrue(is_within(self.root, got))
        self.assertIn("f.txt", str(got))
        self.assertNotIn("..", str(got).replace("\\\\?\\", ""))

    def test_relative_with_base_resolved(self):
        got = canonical_path("a/b/c.txt", base=self.root)
        self.assertTrue(is_within(self.root, got))
        self.assertTrue(got.is_absolute())

    def test_absolute_inside_base_allowed(self):
        inner = self.root / "x" / "y.txt"
        got = canonical_path(inner, base=self.root)
        self.assertTrue(is_within(self.root, got))

    def test_absolute_outside_base_rejected(self):
        outside = self.root.parent / "definitivamente_fora_w7"
        with self.assertRaises(PathSafetyError):
            canonical_path(outside, base=self.root)

    def test_unicode_preserved(self):
        name = "ação_漢字_é.txt"
        got = canonical_path(self.root / "sub" / name)
        self.assertIn(name, str(got))

    @unittest.skipUnless(IS_WIN, "prefixo estendido é comportamento Win32")
    def test_win32_drive_gets_extended_prefix(self):
        got = str(canonical_path("C:\\foo\\bar"))
        self.assertEqual(got, "\\\\?\\C:\\foo\\bar")

    @unittest.skipUnless(IS_WIN, "prefixo estendido é comportamento Win32")
    def test_win32_unc_gets_unc_prefix(self):
        got = str(canonical_path("\\\\server\\share\\dir\\f.txt"))
        self.assertEqual(got, "\\\\?\\UNC\\server\\share\\dir\\f.txt")

    @unittest.skipUnless(IS_WIN, "prefixo estendido é comportamento Win32")
    def test_win32_existing_prefix_not_duplicated(self):
        already = "\\\\?\\C:\\foo\\bar"
        got = str(canonical_path(already))
        self.assertEqual(got, already)
        unc = "\\\\?\\UNC\\srv\\shr\\f"
        self.assertEqual(str(canonical_path(unc)), unc)

    @unittest.skipUnless(IS_WIN, "prefixo estendido é comportamento Win32")
    def test_win32_extended_idempotent_twice(self):
        once = str(canonical_path("C:\\a\\b"))
        twice = str(canonical_path(once))
        self.assertEqual(once, twice)

    @unittest.skipUnless(IS_WIN, "prefixo estendido é comportamento Win32")
    def test_win32_extended_false_returns_plain(self):
        got = str(canonical_path("C:\\foo\\bar", extended=False))
        self.assertEqual(got, "C:\\foo\\bar")

    @unittest.skipUnless(IS_WIN, "separadores Win32")
    def test_win32_forward_slashes_normalized(self):
        got = str(canonical_path("C:/foo/bar", extended=False))
        self.assertEqual(got, "C:\\foo\\bar")

    @unittest.skipIf(IS_WIN, "forma POSIX só é validada fora do Windows")
    def test_posix_normalization(self):
        self.assertEqual(str(canonical_path("/a/b/../c")), "/a/c")
        self.assertEqual(str(canonical_path("/a//b/./c")), "/a/b/c")

    @unittest.skipIf(IS_WIN, "forma POSIX só é validada fora do Windows")
    def test_posix_no_win32_prefix(self):
        got = str(canonical_path("/tmp/x"))
        self.assertFalse(got.startswith("\\\\"))


# ---------------------------------------------------------------------------
# 3. assert_safe_relative
# ---------------------------------------------------------------------------


class TestAssertSafeRelative(unittest.TestCase):
    def test_accepts_plain_relative(self):
        self.assertEqual(assert_safe_relative("a/b/c.py"), "a/b/c.py")
        self.assertEqual(assert_safe_relative("solo.txt"), "solo.txt")

    def test_accepts_pure_path_and_normalizes_sep(self):
        self.assertEqual(assert_safe_relative(PurePosixPath("a/b/c.py")), "a/b/c.py")
        self.assertEqual(
            assert_safe_relative(PureWindowsPath("a\\b\\c.py")), "a/b/c.py"
        )
        self.assertEqual(assert_safe_relative("a\\b\\c.py"), "a/b/c.py")

    def test_rejects_absolute(self):
        for bad in ("/etc/passwd", "\\\\srv\\shr\\f", "C:\\x\\y", "C:/x/y",
                    PureWindowsPath("C:/x/y")):
            with self.subTest(bad=bad):
                with self.assertRaises(PathSafetyError):
                    assert_safe_relative(bad)

    def test_rejects_dotdot(self):
        for bad in ("..", "../x", "a/../b", "a/b/../..", "..\\x", "a\\..\\b"):
            with self.subTest(bad=bad):
                with self.assertRaises(PathSafetyError):
                    assert_safe_relative(bad)

    def test_rejects_empty_and_blank_components(self):
        for bad in ("", ".", "a//b", "a/./b", "/a", "a/", " ", "a/ /b"):
            with self.subTest(bad=bad):
                with self.assertRaises(PathSafetyError):
                    assert_safe_relative(bad)

    def test_rejects_reserved_components(self):
        for bad in ("nul", "a/nul/b", "CON.txt", "dir/aux /f", "com3/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(PathSafetyError):
                    assert_safe_relative(bad)

    def test_rejects_colon_ads(self):
        with self.assertRaises(PathSafetyError):
            assert_safe_relative("arquivo:stream")

    def test_normal_names_pass(self):
        for good in ("normal.py", "com10/x", "null.yml", "console.txt",
                     "a.b/c.d/e"):
            with self.subTest(good=good):
                self.assertEqual(assert_safe_relative(good), good)


# ---------------------------------------------------------------------------
# 4. is_within
# ---------------------------------------------------------------------------


class TestIsWithin(TempTreeTest):
    def test_inside_true(self):
        cand = self.root / "sub" / "f.txt"
        self.assertTrue(is_within(self.root, cand))

    def test_equal_is_within(self):
        self.assertTrue(is_within(self.root, self.root))

    def test_outside_false(self):
        outside = self.root.parent / "w7_fora" / "f.txt"
        self.assertFalse(is_within(self.root, outside))

    def test_sibling_with_common_prefix_false(self):
        repo = self.root / "repo"
        repo2 = self.root / "repo2" / "file.txt"
        self.assertFalse(is_within(repo, repo2))

    @unittest.skipUnless(IS_WIN, "normcase Win32")
    def test_win32_case_insensitive_and_prefix_sibling(self):
        self.assertTrue(is_within(Path("C:\\Repo"), Path("c:\\repo\\f.txt")))
        self.assertFalse(
            is_within(Path("C:\\repo"), Path("C:\\repo2\\file.txt"))
        )

    @unittest.skipIf(IS_WIN, "separadores POSIX")
    def test_posix_sibling_prefix(self):
        self.assertTrue(is_within(Path("/repo"), Path("/repo/a/b")))
        self.assertFalse(is_within(Path("/repo"), Path("/repo2/file")))
        self.assertFalse(is_within(Path("/repo"), Path("/repoX")))


# ---------------------------------------------------------------------------
# 5. iter_tree
# ---------------------------------------------------------------------------


class TestIterTree(TempTreeTest):
    def _rels(self, entries):
        return [e.relative_path for e in entries]

    def test_deterministic_order_and_kinds(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "inner.txt").write_text("i", encoding="utf-8")
        for name in ("C.txt", "a.txt", "B.txt"):
            (self.root / name).write_text(name, encoding="utf-8")

        entries = list(iter_tree(self.root))
        rels = self._rels(entries)
        # casefold: a.txt < b.txt < c.txt < sub ; filhos de sub vêm depois (BFS)
        self.assertEqual(rels, ["a.txt", "B.txt", "C.txt", "sub", "sub/inner.txt"])
        kinds = {e.relative_path: e.kind for e in entries}
        self.assertEqual(kinds["a.txt"], "file")
        self.assertEqual(kinds["sub"], "directory")
        self.assertEqual(kinds["sub/inner.txt"], "file")
        # determinismo: segunda varredura devolve a mesma sequência
        self.assertEqual(self._rels(list(iter_tree(self.root))), rels)

    def test_relative_path_uses_forward_slash(self):
        (self.root / "d1" / "d2").mkdir(parents=True)
        (self.root / "d1" / "d2" / "f.txt").write_text("x", encoding="utf-8")
        rels = self._rels(list(iter_tree(self.root)))
        self.assertIn("d1/d2/f.txt", rels)
        for e in rels:
            self.assertNotIn("\\", e)

    def test_size_mtime_for_files(self):
        payload = b"conteudo-123"
        (self.root / "f.bin").write_bytes(payload)
        (e,) = [e for e in iter_tree(self.root) if e.kind == "file"]
        st = os.lstat(self.root / "f.bin")
        self.assertEqual(e.size, len(payload))
        self.assertEqual(e.mtime_ns, st.st_mtime_ns)
        self.assertEqual(e.path, Path(self.root / "f.bin"))

    def test_prune_names_emitted_as_skipped(self):
        (self.root / ".GIT").mkdir()
        (self.root / ".GIT" / "objects").mkdir()
        (self.root / "keep").mkdir()
        (self.root / "keep" / "z.txt").write_text("z", encoding="utf-8")
        entries = list(iter_tree(self.root, prune_names=frozenset({".git"})))
        rels = self._rels(entries)
        by_rel = {e.relative_path: e for e in entries}
        self.assertEqual(by_rel[".GIT"].kind, "skipped")
        self.assertEqual(by_rel[".GIT"].reason, "pruned")
        self.assertFalse(any(r.startswith(".GIT/") for r in rels))
        self.assertIn("keep/z.txt", rels)

    def test_max_depth_respected_and_visible(self):
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "deep.txt").write_text("d", encoding="utf-8")
        (self.root / "a" / "mid.txt").write_text("m", encoding="utf-8")
        (self.root / "top.txt").write_text("t", encoding="utf-8")

        entries = list(iter_tree(self.root, max_depth=2))
        by_rel = {e.relative_path: e for e in entries}
        self.assertIn("top.txt", by_rel)
        self.assertIn("a/mid.txt", by_rel)
        self.assertEqual(by_rel["a"].kind, "directory")
        # b está em depth 2 >= max_depth: skipped visível, nunca atravessado
        self.assertEqual(by_rel["a/b"].kind, "skipped")
        self.assertEqual(by_rel["a/b"].reason, "max_depth")
        self.assertNotIn("a/b/deep.txt", by_rel)

    def test_max_depth_one_lists_only_top_level(self):
        (self.root / "d").mkdir()
        (self.root / "d" / "x.txt").write_text("x", encoding="utf-8")
        (self.root / "f.txt").write_text("f", encoding="utf-8")
        by_rel = {e.relative_path: e for e in iter_tree(self.root, max_depth=1)}
        self.assertEqual(by_rel["f.txt"].kind, "file")
        self.assertEqual(by_rel["d"].kind, "skipped")
        self.assertEqual(by_rel["d"].reason, "max_depth")
        self.assertNotIn("d/x.txt", by_rel)

    def test_invalid_max_depth(self):
        with self.assertRaises(ValueError):
            list(iter_tree(self.root, max_depth=0))

    def test_vanished_directory_becomes_error_entry(self):
        (self.root / "d").mkdir()
        (self.root / "z.txt").write_text("z", encoding="utf-8")
        entries = []
        for e in iter_tree(self.root):
            entries.append(e)
            if e.relative_path == "d" and e.kind == "directory":
                shutil.rmtree(self.root / "d")
        errors = [e for e in entries if e.kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].relative_path, "d")
        self.assertIn("Error", errors[0].reason)

    def test_missing_root_becomes_error_entry(self):
        missing = self.root / "nao_existe_w7"
        entries = list(iter_tree(missing))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "error")
        self.assertEqual(entries[0].relative_path, ".")

    def test_file_root_rejected(self):
        f = self.root / "f.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(PathSafetyError):
            list(iter_tree(f))

    def test_symlink_dir_not_followed_posix(self):
        if IS_WIN:
            self.skipTest("teste POSIX puro de symlink")
        real = self.root / "real"
        real.mkdir()
        (real / "loop").symlink_to(real, target_is_directory=True)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "segredo.txt").write_text("s", encoding="utf-8")
        scan_root = self.root / "scan"
        scan_root.mkdir()
        (scan_root / "ok.txt").write_text("o", encoding="utf-8")
        (scan_root / "esc").symlink_to(outside, target_is_directory=True)

        entries = list(iter_tree(scan_root))
        by_rel = {e.relative_path: e for e in entries}
        self.assertEqual(by_rel["esc"].kind, "skipped")
        self.assertEqual(by_rel["esc"].reason, "reparse_point")
        self.assertFalse(any(r.startswith("esc/") for r in by_rel))
        # junction/symlink cíclico termina em skipped, sem recursão
        entries2 = list(iter_tree(real))
        by_rel2 = {e.relative_path: e for e in entries2}
        self.assertEqual(by_rel2["loop"].kind, "skipped")
        self.assertEqual(by_rel2["loop"].reason, "reparse_point")
        self.assertFalse(any(r.startswith("loop/") for r in by_rel2))

    def test_symlink_root_rejected_posix(self):
        if IS_WIN:
            self.skipTest("teste POSIX puro de symlink")
        target = self.root / "alvo"
        target.mkdir()
        link = self.root / "link_root"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(PathSafetyError):
            list(iter_tree(link))

    @unittest.skipUnless(IS_WIN, "junction exige Windows (mklink /J)")
    def test_win32_junction_cyclic_and_external_not_followed(self):
        real = self.root / "real"
        real.mkdir()
        (real / "f.txt").write_text("x", encoding="utf-8")
        loop = real / "loop"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "segredo.txt").write_text("s", encoding="utf-8")
        scan_root = self.root / "scan"
        scan_root.mkdir()
        (scan_root / "ok.txt").write_text("o", encoding="utf-8")
        esc = scan_root / "esc"

        res = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(loop), str(real)],
            capture_output=True)
        self.assertEqual(res.returncode, 0, "mklink /J para junction cíclica falhou")
        res = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(esc), str(outside)],
            capture_output=True)
        self.assertEqual(res.returncode, 0, "mklink /J para junction externa falhou")
        # cleanup explícito dos reparse points antes do TemporaryDirectory
        self.addCleanup(
            lambda: os.rmdir(esc) if os.path.lexists(esc) else None
        )
        self.addCleanup(
            lambda: os.rmdir(loop) if os.path.lexists(loop) else None
        )

        entries = list(iter_tree(scan_root))
        by_rel = {e.relative_path: e for e in entries}
        self.assertEqual(by_rel["esc"].kind, "skipped")
        self.assertEqual(by_rel["esc"].reason, "reparse_point")
        # conteúdo do alvo fora do root nunca aparece
        self.assertFalse(any(r.startswith("esc/") for r in by_rel))
        self.assertIn("ok.txt", by_rel)

        # junction cíclica termina em skipped — prova: varredura termina
        entries2 = list(iter_tree(real))
        by_rel2 = {e.relative_path: e for e in entries2}
        self.assertEqual(by_rel2["loop"].kind, "skipped")
        self.assertEqual(by_rel2["loop"].reason, "reparse_point")
        self.assertFalse(any(r.startswith("loop/") for r in by_rel2))
        self.assertIn("f.txt", by_rel2)

    @unittest.skipUnless(IS_WIN, "junction exige Windows (mklink /J)")
    def test_win32_junction_root_rejected(self):
        target = self.root / "alvo"
        target.mkdir()
        link = self.root / "link_root"
        res = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True)
        self.assertEqual(res.returncode, 0)
        self.addCleanup(
            lambda: os.rmdir(link) if os.path.lexists(link) else None
        )
        with self.assertRaises(PathSafetyError):
            list(iter_tree(link))


# ---------------------------------------------------------------------------
# 6. sha256_file
# ---------------------------------------------------------------------------


class TestSha256File(TempTreeTest):
    def test_known_hash_and_metadata(self):
        payload = b"henke-wave7-" * 100
        f = self.root / "dados.bin"
        f.write_bytes(payload)
        digest = sha256_file(f)
        self.assertIsInstance(digest, FileDigest)
        self.assertEqual(digest.sha256, hashlib.sha256(payload).hexdigest())
        st = os.lstat(f)
        self.assertEqual(digest.size, len(payload))
        self.assertEqual(digest.mtime_ns, st.st_mtime_ns)

    def test_small_chunk_size_same_hash(self):
        payload = bytes(range(256)) * 4
        f = self.root / "chunks.bin"
        f.write_bytes(payload)
        digest = sha256_file(f, chunk_size=7)
        self.assertEqual(digest.sha256, hashlib.sha256(payload).hexdigest())

    def test_invalid_chunk_size(self):
        f = self.root / "f.bin"
        f.write_bytes(b"x")
        with self.assertRaises(ValueError):
            sha256_file(f, chunk_size=0)

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(FileNotFoundError):
            sha256_file(self.root / "inexistente.bin")

    def test_directory_rejected(self):
        d = self.root / "dir"
        d.mkdir()
        with self.assertRaises(PathSafetyError):
            sha256_file(d)

    def test_unstable_file_detected(self):
        """Fault injection determinística: segundo stat reporta mtime_ns novo."""
        f = self.root / "mutavel.bin"
        f.write_bytes(b"estavel" * 32)
        real_lstat = os.lstat
        calls = {"n": 0}

        def fake_lstat(p):
            calls["n"] += 1
            st = real_lstat(p)
            if calls["n"] == 2:
                return SimpleNamespace(
                    st_mode=st.st_mode,
                    st_size=st.st_size,
                    st_mtime_ns=st.st_mtime_ns + 1,
                    st_file_attributes=getattr(st, "st_file_attributes", 0),
                )
            return st

        with patch("scripts.winpath._lstat", side_effect=fake_lstat):
            with self.assertRaises(UnstableFileError):
                sha256_file(f)
        self.assertEqual(calls["n"], 2)

    def test_unstable_size_change_detected(self):
        f = self.root / "mutavel2.bin"
        f.write_bytes(b"12345678")
        real_lstat = os.lstat
        calls = {"n": 0}

        def fake_lstat(p):
            calls["n"] += 1
            st = real_lstat(p)
            if calls["n"] == 2:
                return SimpleNamespace(
                    st_mode=st.st_mode,
                    st_size=st.st_size + 16,
                    st_mtime_ns=st.st_mtime_ns,
                    st_file_attributes=getattr(st, "st_file_attributes", 0),
                )
            return st

        with patch("scripts.winpath._lstat", side_effect=fake_lstat):
            with self.assertRaises(UnstableFileError):
                sha256_file(f)

    def test_symlink_file_rejected_posix(self):
        if IS_WIN:
            self.skipTest("symlink de arquivo POSIX")
        target = self.root / "alvo.txt"
        target.write_text("x", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(target)
        with self.assertRaises(PathSafetyError):
            sha256_file(link)

    @unittest.skipUnless(IS_WIN, "symlink de arquivo Win32 exige privilégio")
    def test_win32_file_symlink_rejected(self):
        target = self.root / "alvo.txt"
        target.write_text("x", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"sem privilégio para symlink de arquivo: {exc}")
        self.addCleanup(
            lambda: os.remove(link) if os.path.lexists(link) else None
        )
        with self.assertRaises(PathSafetyError):
            sha256_file(link)

    def test_digest_is_frozen(self):
        f = self.root / "f.bin"
        f.write_bytes(b"abc")
        digest = sha256_file(f)
        with self.assertRaises(Exception):
            digest.sha256 = "0" * 64


# ---------------------------------------------------------------------------
# 7. Caminho longo (>260) no Windows
# ---------------------------------------------------------------------------


class TestLongPathWin32(TempTreeTest):
    @unittest.skipUnless(IS_WIN, "caminho >260 exige Windows com \\\\?\\")
    def test_long_path_create_and_hash(self):
        deep = self.root / "w7long"
        while len(str(deep)) < 300:
            deep = deep / ("d" * 40)
        ext_deep = canonical_path(deep)
        self.assertTrue(str(ext_deep).startswith("\\\\?\\"))
        os.makedirs(str(ext_deep))
        self.addCleanup(
            lambda: shutil.rmtree(
                str(canonical_path(self.root / "w7long")),
                ignore_errors=False,
            )
            if os.path.lexists(self.root / "w7long")
            else None
        )

        payload = b"longpath-payload" * 64
        f = deep / "payload.bin"
        self.assertGreater(len(str(f)), 260)
        ext_f = canonical_path(f)
        with open(str(ext_f), "wb") as fh:
            fh.write(payload)

        digest = sha256_file(ext_f)
        self.assertEqual(digest.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(digest.size, len(payload))

        # iter_tree também alcança o arquivo pela forma estendida
        found = [e for e in iter_tree(ext_deep) if e.kind == "file"]
        self.assertEqual([e.relative_path for e in found], ["payload.bin"])

    @unittest.skipUnless(IS_WIN, "\\\\?\\ preserva idempotência em caminho longo")
    def test_long_path_canonical_idempotent(self):
        deep = self.root / ("x" * 200) / ("y" * 100)
        once = canonical_path(deep)
        twice = canonical_path(once)
        self.assertEqual(once, twice)


# ---------------------------------------------------------------------------
# Contratos dos tipos
# ---------------------------------------------------------------------------


class TestDataclassContracts(unittest.TestCase):
    def test_tree_entry_defaults(self):
        e = TreeEntry(Path("/x"), "x", "file", 1, 2)
        self.assertEqual(e.reason, "")
        with self.assertRaises(Exception):
            e.kind = "error"


if __name__ == "__main__":
    unittest.main()
