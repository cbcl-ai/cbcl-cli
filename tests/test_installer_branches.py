"""Hermetic installer branch tests: no package, Docker or network mutation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARBALL = "https://github.com/cbcl-ai/cbcl-cli/archive/v0.5.29.tar.gz"
GIT = "git+https://github.com/cbcl-ai/cbcl-cli.git@v0.5.29"
STUB = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["INSTALLER_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")
if name == "docker":
    sys.exit(0)
if name == "git":
    raise SystemExit("A package stub must not invoke real Git")
if name.startswith("python"):
    if args == ["--version"]:
        print("Python 3.13.0"); sys.exit(0)
    if args[:1] == ["-c"]:
        sys.exit(0)
    if args[:2] == ["-m", "venv"]:
        target = pathlib.Path(args[2]) / "bin"
        target.mkdir(parents=True)
        pip = target / "pip"
        pip.write_text(pathlib.Path(sys.argv[0]).read_text())
        pip.chmod(0o700)
        sys.exit(0)
    assert args[:2] == ["-m", "pip"], args
if name == "pipx" and args[:1] == ["ensurepath"]:
    sys.exit(0)
source = args[-1]
assert source.startswith(("https://github.com/", "git+https://github.com/")), source
if os.environ.get("INSTALLER_TEST_MODE") == "pep668":
    print("externally-managed-environment: use a virtual environment", file=sys.stderr)
    sys.exit(1)
if os.environ.get("INSTALLER_TEST_MODE") == "both_fail":
    print("synthetic package failure for " + source, file=sys.stderr)
    sys.exit(1)
if os.environ.get("INSTALLER_TEST_MODE") == "tarball_fail" and source.startswith("https:"):
    print("synthetic archive download failure", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
'''


class InstallerTests(unittest.TestCase):
    def run_installer(self, *, branch="user", mode="ok", git=False):
        with tempfile.TemporaryDirectory(prefix="cbcl-installer-test-") as directory:
            base = Path(directory)
            binaries, temporary = base / "bin", base / "temporary"
            binaries.mkdir()
            temporary.mkdir()
            for name in ["python3.13", "docker", *(["pipx"] if branch == "pipx" else []),
                         *(["git"] if git else [])]:
                stub = binaries / name
                stub.write_text(f"#!{sys.executable}\n" + STUB)
                stub.chmod(0o700)
            for name in ["mktemp", "rm", "grep", "cat", "sed"]:
                target = shutil.which(name)
                self.assertIsNotNone(target)
                (binaries / name).symlink_to(target)
            calls_file = base / "calls.jsonl"
            environment = {**os.environ, "PATH": str(binaries), "TMPDIR": str(temporary),
                           "INSTALLER_TEST_CALLS": str(calls_file),
                           "INSTALLER_TEST_MODE": mode}
            # HOME is retained. Every program that could install/write user
            # state is stubbed; no pip/pipx/venv/Docker command escapes this PATH.
            script = ROOT / "install.sh"
            command = ["/bin/bash", str(script), "--ref", "v0.5.29"]
            if branch == "venv":
                command += ["--venv", str(base / "virtual environment")]
            result = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, timeout=10, check=False)
            calls = [json.loads(row) for row in calls_file.read_text().splitlines()]
            package_calls = [row for row in calls if any(
                value.startswith(("https://github.com/", "git+https://github.com/"))
                for value in row
            )]
            self.assertEqual(list(temporary.iterdir()), [], "temporary pip log leaked")
            return result, package_calls, calls


    def test_user_tarball_succeeds_without_git_or_pipx(self):
        result, calls, _ = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[-1] for call in calls], [TARBALL])
        self.assertEqual(calls[0][1:-1], ["-m", "pip", "install", "--user", "--upgrade", "--quiet"])
        self.assertIn("Done. Pair this machine", result.stdout)

    def test_user_archive_failure_falls_back_to_exact_git_ref(self):
        result, calls, _ = self.run_installer(mode="tarball_fail", git=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[-1] for call in calls], [TARBALL, GIT])

    def test_user_both_sources_fail_truthfully(self):
        result, calls, _ = self.run_installer(mode="both_fail", git=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[-1] for call in calls], [TARBALL, GIT])
        self.assertIn("pip install failed", result.stderr)
        self.assertIn("synthetic package failure", result.stderr)
        self.assertNotIn("Done. Pair this machine", result.stdout)

    def test_user_pep668_preserves_guidance_without_pointless_git_retry(self):
        result, calls, _ = self.run_installer(mode="pep668", git=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[-1] for call in calls], [TARBALL])
        self.assertIn("PEP 668", result.stderr)
        self.assertIn("--venv", result.stderr)
        self.assertNotIn("Done. Pair this machine", result.stdout)

    def test_pipx_tarball_and_git_fallback_remain_intact(self):
        for mode, sources in [("ok", [TARBALL]), ("tarball_fail", [TARBALL, GIT])]:
            with self.subTest(mode=mode):
                result, calls, all_calls = self.run_installer(branch="pipx", mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call[-1] for call in calls], sources)
                self.assertTrue(all(call[1:-1] == ["install", "--force"] for call in calls))
                self.assertIn(["pipx", "ensurepath", "--force"], all_calls)

    def test_explicit_venv_tarball_and_git_fallback_remain_intact(self):
        for mode, sources in [("ok", [TARBALL]), ("tarball_fail", [TARBALL, GIT])]:
            with self.subTest(mode=mode):
                result, calls, _ = self.run_installer(branch="venv", mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call[-1] for call in calls], sources)
                self.assertTrue(all(call[1:-1] == ["install", "--upgrade", "--quiet"] for call in calls))
                self.assertIn("virtual environment/bin/cbcl setup", result.stdout)

    def test_pipx_and_venv_failed_installs_never_report_success(self):
        for branch in ["pipx", "venv"]:
            with self.subTest(branch=branch):
                result, calls, _ = self.run_installer(branch=branch, mode="both_fail")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([call[-1] for call in calls], [TARBALL, GIT])
                self.assertNotIn("Done. Pair this machine", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
