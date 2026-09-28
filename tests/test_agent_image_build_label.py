"""Every builder of the agent image labels it with the daemon's cache key.

``ensure_image`` rebuilds ``cbcl-agent`` whenever the image's
``mcp_server_hash`` label differs from ``_compute_mcp_server_hash()``, and
the runtime evals refuse an image whose label does not match. ``build.sh``
(the manual helper) used to build without the label, so its image was always
reported stale and rebuilt. These tests run ``build.sh`` against a recording
``docker`` stub and pin that it passes the SAME key the daemon compares.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src._agent_image import image_hash
from src.docker.container_manager import _DOCKER_DIR, _compute_mcp_server_hash

_BUILD_SH = _DOCKER_DIR / "build.sh"


def _run_build_sh(tmp_path: Path, *args: str) -> list[str]:
    """Run build.sh with a fake ``docker`` that records its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "docker-args"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'for arg in "$@"; do printf "%s\\n" "$arg" >> "{record}"; done\n'
    )
    fake.chmod(0o755)
    # ``python3`` for build.sh's hash step: the interpreter running the tests.
    (bin_dir / "python3").symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    subprocess.run(
        ["bash", str(_BUILD_SH), *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return record.read_text().splitlines()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_build_sh_labels_the_image_with_the_daemon_cache_key(tmp_path: Path) -> None:
    argv = _run_build_sh(tmp_path, "cbcl-agent:eval")

    assert argv[0] == "build"
    assert "--label" in argv
    label = argv[argv.index("--label") + 1]
    assert label == f"{image_hash.LABEL}={_compute_mcp_server_hash()}"
    assert argv[argv.index("-t") + 1] == "cbcl-agent:eval"
    assert argv[-1] == str(_DOCKER_DIR)


def test_daemon_and_build_script_share_one_key() -> None:
    assert _compute_mcp_server_hash() == image_hash.compute_image_hash(_DOCKER_DIR)


def test_image_hash_script_prints_the_daemon_key() -> None:
    """``build.sh`` runs the module as an isolated script (no package
    imports); its output must be the daemon's key."""
    result = subprocess.run(
        [sys.executable, "-I", str(_DOCKER_DIR / "image_hash.py")],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == _compute_mcp_server_hash()


def test_image_hash_script_prints_the_whole_label() -> None:
    """``--label`` is what ``build.sh`` passes to ``docker build --label``:
    the key comes from ``image_hash.LABEL``, never a second spelling."""
    result = subprocess.run(
        [sys.executable, "-I", str(_DOCKER_DIR / "image_hash.py"), "--label"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == f"{image_hash.LABEL}={_compute_mcp_server_hash()}"
    assert "mcp_server_hash" not in _BUILD_SH.read_text()


def _layout(root: Path, files: dict[str, str]) -> Path:
    (root / "_mcp").mkdir(parents=True)
    (root / "Dockerfile.agent").write_text("FROM scratch\n")
    for name, text in files.items():
        (root / name).write_text(text)
    return root


def test_moving_content_between_files_changes_the_key(tmp_path: Path) -> None:
    """Concatenated bytes alone cannot tell ``a|bc`` from ``ab|c``; the key
    frames each file by name and length."""
    first = _layout(
        tmp_path / "one",
        {"_mcp/tools_a.py": "x = 1\ny = 2\n", "_mcp/tools_b.py": "z = 3\n"},
    )
    second = _layout(
        tmp_path / "two",
        {"_mcp/tools_a.py": "x = 1\n", "_mcp/tools_b.py": "y = 2\nz = 3\n"},
    )
    assert image_hash.compute_image_hash(first) != image_hash.compute_image_hash(
        second
    )


def test_renaming_a_package_module_changes_the_key(tmp_path: Path) -> None:
    first = _layout(tmp_path / "one", {"_mcp/tools_a.py": "x = 1\n"})
    second = _layout(tmp_path / "two", {"_mcp/tools_b.py": "x = 1\n"})
    assert image_hash.compute_image_hash(first) != image_hash.compute_image_hash(
        second
    )


def test_every_file_in_the_copied_package_is_hashed() -> None:
    """``COPY _mcp`` ships the whole directory, the key hashes its ``*.py``.
    A non-Python file there (data a module reads) would be baked in without
    invalidating the image; extend ``image_source_files`` before adding one."""
    package = _DOCKER_DIR / "_mcp"
    shipped = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != ".DS_Store"
    }
    hashed = {
        path.relative_to(package).as_posix()
        for path in image_hash.image_source_files(_DOCKER_DIR)
        if package in path.parents
    }
    assert shipped == hashed
