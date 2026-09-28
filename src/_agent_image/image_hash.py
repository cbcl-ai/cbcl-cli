"""The agent image's cache key: the files baked into ``cbcl-agent`` and their hash.

ONE definition shared by every builder of the image. The daemon
(``src/docker/container_manager.py``: ``ensure_image`` / ``_build_image``)
stores the hash as the image label ``mcp_server_hash`` and rebuilds when the
label differs from the current sources; ``build.sh`` runs this file as a
script (``--label``) to put the same label on a manually built image; the
runtime evals
compare a local image's label against it. If a builder computed the key
from a different file set, an image could be judged current while it ships
stale helpers, or be rebuilt on every start.

Stdlib only and free of package imports, so ``python3 image_hash.py`` works
from ``build.sh`` with any interpreter, outside the daemon's environment.

The source set must equal the ``COPY`` set of ``Dockerfile.agent``;
``tests/test_agent_image_copy_sync.py`` enforces that.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

IMAGE_DIR = Path(__file__).resolve().parent
LABEL = "mcp_server_hash"

# Single files COPYed into the image (``/opt/cubicle`` and
# ``/usr/local/libexec/cubicle``), relative to ``IMAGE_DIR``.
_SOURCE_FILES = (
    "mcp_tool_server.py",
    "_mcp_backend.py",
    "_mcp_script_exec.py",
    "bash_guard.py",
    "execution_pace.py",
    "secure_files.py",
    "generation_runner.py",
    "generation_sources.py",
)
# Directories COPYed whole; their Python modules are hashed.
_SOURCE_PACKAGES = ("_mcp",)


def image_source_files(image_dir: Path = IMAGE_DIR) -> list[Path]:
    """Every source file baked into the image, in a stable order.

    Excludes ``Dockerfile.agent`` itself: it is the build recipe, hashed
    first by ``compute_image_hash``, not a COPYed artifact.
    """
    files = [image_dir / name for name in _SOURCE_FILES]
    for package in _SOURCE_PACKAGES:
        package_dir = image_dir / package
        if package_dir.is_dir():
            files.extend(sorted(package_dir.glob("*.py")))
    return files


def compute_image_hash(image_dir: Path = IMAGE_DIR) -> str:
    """Hash the image's build inputs: the Dockerfile, then every source file.

    Each file is framed by its relative path and length, so moving content
    between files, or renaming one, changes the key even when the
    concatenated bytes would not. Returns the first 12 hex characters of an
    MD5: this invalidates a cache, it does not authenticate anything.
    """
    digest = hashlib.md5()
    for path in [image_dir / "Dockerfile.agent", *image_source_files(image_dir)]:
        name = path.relative_to(image_dir).as_posix().encode()
        if not path.exists():
            digest.update(b"missing\0" + name + b"\0")
            continue
        data = path.read_bytes()
        digest.update(b"file\0" + name + b"\0" + str(len(data)).encode() + b"\0")
        digest.update(data)
    return digest.hexdigest()[:12]


if __name__ == "__main__":
    # ``--label`` prints the whole ``<LABEL>=<hash>`` image label, so
    # ``build.sh`` never spells the label key itself.
    key = compute_image_hash()
    sys.stdout.write((f"{LABEL}={key}" if "--label" in sys.argv[1:] else key) + "\n")
