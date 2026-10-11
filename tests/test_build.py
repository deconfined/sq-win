import locale
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_project import apply_patch, check_lock_change, run, seed_patched_lock


class LockedPatchTests(unittest.TestCase):
    LOCK = b'''version = 4

[[package]]
name = "sequoia-gpg-agent"
version = "0.6.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "original-checksum"
dependencies = ["thiserror 2.0.18"]

[[package]]
name = "thiserror"
version = "1.0.69"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "first-checksum"

[[package]]
name = "thiserror"
version = "2.0.18"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "second-checksum"
'''

    def test_path_patch_preserves_the_locked_edge_with_an_alternative_version_present(self):
        patched = seed_patched_lock(self.LOCK, {("sequoia-gpg-agent", "0.6.2")})
        self.assertIn(b'dependencies = ["thiserror 2.0.18"]', patched)
        self.assertIn(b'checksum = "first-checksum"', patched)
        self.assertIn(b'checksum = "second-checksum"', patched)
        self.assertNotIn(b'checksum = "original-checksum"', patched)
        check_lock_change(self.LOCK, patched, {("sequoia-gpg-agent", "0.6.2")})

    def test_guard_rejects_reselecting_an_existing_locked_dependency(self):
        patched = seed_patched_lock(self.LOCK, {("sequoia-gpg-agent", "0.6.2")})
        changed = patched.replace(b'dependencies = ["thiserror 2.0.18"]',
                                  b'dependencies = ["thiserror 1.0.69"]')
        with self.assertRaises(RuntimeError):
            check_lock_change(self.LOCK, changed, {("sequoia-gpg-agent", "0.6.2")})

    def test_unknown_or_ambiguous_patch_sources_are_rejected(self):
        with self.assertRaises(ValueError):
            seed_patched_lock(self.LOCK, {("sequoia-gpg-agent", "0.6.0")})
        with self.assertRaises(ValueError):
            seed_patched_lock(self.LOCK + self.LOCK[self.LOCK.index(b'[[package]]'):],
                              {("sequoia-gpg-agent", "0.6.2")})


class ProcessEncodingTests(unittest.TestCase):
    def test_cargo_style_utf8_output_decodes_on_a_windows_code_page(self):
        # Ł contains byte 0x81 in UTF-8, which is undefined in CP1252.
        # Cargo metadata contains Unicode author names and descriptions.
        with mock.patch.object(locale, "getencoding", return_value="cp1252"):
            output = run([sys.executable, "-c",
                          'import os; os.write(1, "Łódź — OpenPGP".encode("utf-8"))'], capture=True)
        self.assertEqual(output, "Łódź — OpenPGP")


class PatchCheckoutTests(unittest.TestCase):
    def test_windows_checkout_keeps_patches_lf_and_applicable_to_upstream_source(self):
        patch_bytes = b'''diff --git a/src/agent.rs b/src/agent.rs
--- a/src/agent.rs
+++ b/src/agent.rs
@@ -1,4 +1,4 @@
 fn use_native_agent() {
-    let mode = "unix";
+    let mode = "windows";
     println!("{}", mode);
 }
'''
        upstream_bytes = b'''fn use_native_agent() {
    let mode = "unix";
    println!("{}", mode);
}
'''
        # Ignore developer Git configuration so autocrlf=true exercises the
        # same checkout conversion on Linux and on a native Windows runner.
        git_env = os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            checkout = directory / "pipeline"
            checkout.mkdir()

            def git(*arguments):
                return subprocess.run(["git", *arguments], cwd=checkout, env=git_env,
                                      check=True, capture_output=True, encoding="utf-8")

            git("init", "--quiet")
            git("config", "core.autocrlf", "true")
            attributes = Path(__file__).resolve().parents[1] / ".gitattributes"
            (checkout / ".gitattributes").write_bytes(attributes.read_bytes())
            patch = checkout / "native-agent.patch"
            patch.write_bytes(patch_bytes)
            ordinary_file = checkout / "ordinary.txt"
            ordinary_file.write_bytes(b"ordinary text\n")
            git("add", ".gitattributes", patch.name, ordinary_file.name)
            git("-c", "user.name=Sequoia Build Tests", "-c", "user.email=fixtures@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Checkout fixture")
            # Force a real Git checkout of both tracked files, as cloning the
            # pipeline on Windows would. All mutations stay in this fixture.
            patch.unlink()
            ordinary_file.unlink()
            git("restore", "--worktree", "--", patch.name, ordinary_file.name)
            self.assertEqual(ordinary_file.read_bytes(), b"ordinary text\r\n")
            self.assertEqual(patch.read_bytes(), patch_bytes)

            upstream = directory / "upstream"
            (upstream / "src").mkdir(parents=True)
            source = upstream / "src" / "agent.rs"
            source.write_bytes(upstream_bytes)
            self.assertEqual(apply_patch(upstream, patch), "applied")
            self.assertEqual(source.read_bytes(), upstream_bytes.replace(b'"unix"', b'"windows"'))


if __name__ == "__main__":
    unittest.main()
