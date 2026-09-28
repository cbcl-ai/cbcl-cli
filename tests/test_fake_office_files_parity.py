"""The backend's in-memory Office fake answers like the real Files helper (T16).

``backend/tests/fake_office_files.py`` stands in for the container helper in
every backend skill-route test. When it diverged (it spliced an upload chunk
at a mismatched offset and created a file over a folder on
``fs_write_revision``), backend tests could pass against behaviour the
Office never has. This runs the SAME requests through both — the real
``secure_files.execute`` on a Linux root and the fake — and compares the
outcome and the resulting workspace.

It lives in the communicator lane because the real helper needs Linux
(``/proc/self/fdinfo``). The backend package must be importable
(``PYTHONPATH`` includes ``backend``): in the monorepo a missing backend is
an error, and only the standalone CLI mirror (no ``backend/``) skips.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest

from src._agent_image import secure_files as files
from tests.backend_boundary import import_backend

import_backend("app.transport.office_fs")

_FAKE_PATH = (
    Path(__file__).resolve().parents[2] / "backend" / "tests" / "fake_office_files.py"
)


def _load_fake_module():
    spec = importlib.util.spec_from_file_location(
        "backend_fake_office_files", _FAKE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_module = _load_fake_module()

SKILL = ".claude/skills/demo"
_COMPARED_KEYS = (
    "outcome",
    "sha256",
    "previous_sha256",
    "current_sha256",
    "size",
    "bytes_written",
    "total_size",
    "exists",
    "type",
    "code",
    "created",
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Pair:
    """The real helper on ``root`` and the fake, seeded with the same state."""

    def __init__(self, root: Path, files_: dict[str, bytes], dirs: list[str]):
        self.root = root
        for folder in [SKILL, *dirs]:
            (root / folder).mkdir(parents=True, exist_ok=True)
        for path, data in files_.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(data)
        self.fake = fake_module.FakeOfficeFiles(
            dict(files_), revisions=True, bundles=True
        )
        self.fake.dirs.update(
            [SKILL, *dirs, *fake_module._parents(SKILL)]
            + [parent for folder in dirs for parent in fake_module._parents(folder)]
        )

    def run(self, action: str, **params) -> tuple[dict, dict]:
        real = files.execute({"action": action, "params": params}, self.root)
        fake = asyncio.run(self.fake.request(None, action, dict(params)))
        return real, fake

    def assert_same(self, action: str, **params) -> dict:
        real, fake = self.run(action, **params)
        assert _outcome(real) == _outcome(fake), (action, params, real, fake)
        return real

    def assert_same_state(self, *paths: str) -> None:
        for path in paths:
            on_disk = self.root / path
            real = on_disk.read_bytes() if on_disk.is_file() else None
            assert real == self.fake.files.get(path), path


def _outcome(response: dict) -> dict:
    status = response.get("status") if response.get("error") else None
    if status is not None:
        return {"status": status, "code": response.get("code")}
    return {key: response.get(key) for key in _COMPARED_KEYS if key in response}


@pytest.fixture
def pair_factory(tmp_path):
    def build(files_: dict[str, bytes] | None = None, dirs: list[str] | None = None):
        root = tmp_path / f"workspace-{len(list(tmp_path.iterdir()))}"
        return Pair(root, files_ or {}, dirs or [])

    return build


# -- uploads ------------------------------------------------------------------


def test_upload_chunks_append_only_at_the_current_end(pair_factory):
    pair = pair_factory()
    path = f"{SKILL}/blob.bin"
    pair.assert_same("fs_upload_chunk", path=path, offset=0, chunk_base64=_b64(b"abc"))
    pair.assert_same("fs_upload_chunk", path=path, offset=3, chunk_base64=_b64(b"de"))
    pair.assert_same("fs_upload_chunk", path=path, offset=5, chunk_base64="", done=True)
    pair.assert_same_state(path)


@pytest.mark.parametrize("offset", [1, 4, 99])
def test_upload_at_a_mismatched_offset_is_refused(pair_factory, offset):
    path = f"{SKILL}/blob.bin"
    pair = pair_factory({path: b"abc"})
    real = pair.assert_same(
        "fs_upload_chunk", path=path, offset=offset, chunk_base64=_b64(b"zz")
    )
    assert real["status"] == 400
    pair.assert_same_state(path)


def test_final_done_chunk_is_validated_too(pair_factory):
    path = f"{SKILL}/blob.bin"
    pair = pair_factory({path: b"abc"})
    real = pair.assert_same(
        "fs_upload_chunk", path=path, offset=7, chunk_base64="", done=True
    )
    assert real["status"] == 400


def test_later_chunk_for_a_missing_file_is_not_found(pair_factory):
    pair = pair_factory()
    real = pair.assert_same(
        "fs_upload_chunk", path=f"{SKILL}/none.bin", offset=3, chunk_base64=_b64(b"x")
    )
    assert real["status"] == 404


def test_upload_over_a_folder_is_refused(pair_factory):
    pair = pair_factory(dirs=[f"{SKILL}/refs"])
    real = pair.assert_same(
        "fs_upload_chunk", path=f"{SKILL}/refs", offset=0, chunk_base64=_b64(b"x")
    )
    assert real["status"] == 400


# -- revision writes ------------------------------------------------------------


def test_write_revision_on_a_folder_without_precondition_is_refused(pair_factory):
    pair = pair_factory(dirs=[f"{SKILL}/refs"])
    real = pair.assert_same("fs_write_revision", path=f"{SKILL}/refs", content="x")
    assert real["status"] == 400
    assert f"{SKILL}/refs" not in pair.fake.files


@pytest.mark.parametrize(
    "precondition",
    [{"expect_absent": True}, {"expected_sha256": "0" * 64}],
    ids=["expect_absent", "expected_sha256"],
)
def test_write_revision_on_a_folder_with_precondition_conflicts(
    pair_factory, precondition
):
    pair = pair_factory(dirs=[f"{SKILL}/refs"])
    real = pair.assert_same(
        "fs_write_revision", path=f"{SKILL}/refs", content="x", **precondition
    )
    assert real["status"] == 409


def test_write_revision_cas_outcomes(pair_factory):
    path = f"{SKILL}/SKILL.md"
    pair = pair_factory({path: b"old"})
    assert (
        pair.assert_same("fs_write_revision", path=path, content="old")["outcome"]
        == "unchanged"
    )
    assert (
        pair.assert_same(
            "fs_write_revision", path=path, content="new", expect_absent=True
        )["status"]
        == 409
    )
    assert (
        pair.assert_same(
            "fs_write_revision", path=path, content="new", expected_sha256=_sha(b"x")
        )["status"]
        == 409
    )
    assert (
        pair.assert_same(
            "fs_write_revision", path=path, content="new", expected_sha256="NOT-HEX"
        )["status"]
        == 400
    )
    written = pair.assert_same(
        "fs_write_revision", path=path, content="new", expected_sha256=_sha(b"old")
    )
    assert written["outcome"] == "written"
    pair.assert_same_state(path)
    created = pair.assert_same(
        "fs_write_revision", path=f"{SKILL}/fresh.md", content="hi", expect_absent=True
    )
    assert created["outcome"] == "written"
    pair.assert_same_state(path, f"{SKILL}/fresh.md")


# -- reads, hashes, mkdir, delete ----------------------------------------------


def test_reads_hashes_and_errors_agree(pair_factory):
    path = f"{SKILL}/SKILL.md"
    pair = pair_factory({path: b"---\nname: demo\n---\n"}, dirs=[f"{SKILL}/refs"])
    pair.assert_same("fs_read", path=path)
    assert pair.assert_same("fs_read", path=f"{SKILL}/none.md")["status"] == 404
    assert pair.assert_same("fs_read", path=f"{SKILL}/refs")["status"] == 400
    pair.assert_same("fs_hash", path=path)
    pair.assert_same("fs_hash", path=f"{SKILL}/none.md")
    pair.assert_same("fs_hash", path=f"{SKILL}/refs")
    # An existing folder is reported, not refused (a retry after a lost
    # answer); a new one is created; a file in the way is refused.
    assert pair.assert_same("fs_mkdir", path=f"{SKILL}/refs")["created"] is False
    assert pair.assert_same("fs_mkdir", path=f"{SKILL}/new")["created"] is True
    assert pair.assert_same("fs_mkdir", path=path)["status"] == 400
    assert pair.assert_same("fs_delete", path=f"{SKILL}/none.md")["status"] == 404


# -- whole-skill publication (fs_skill_*) -------------------------------------

PUBLICATION = "a" * 32
SKILL_MD = b"---\nname: demo\ndescription: Demo skill. Use when testing.\n---\nbody\n"


def _manifest(data: bytes) -> dict:
    entries = [
        {"path": "SKILL.md", "size": len(data), "sha256": _sha(data), "mode": 0o644}
    ]
    return {"files": entries, "bundle_sha256": files.skill_bundle_sha256(entries)}


def _staged(pair: Pair, skill: str, data: bytes) -> None:
    pair.assert_same(
        "fs_skill_stage_begin", publication_id=PUBLICATION, skill_name=skill
    )
    pair.assert_same(
        "fs_skill_stage_put",
        publication_id=PUBLICATION,
        files=[{"path": "SKILL.md", "offset": 0, "data_base64": _b64(data)}],
    )


def _publication_state(pair: Pair, skill: str) -> None:
    real, fake = pair.run(
        "fs_skill_status", skill_name=skill, publication_id=PUBLICATION
    )
    keys = ("staged", "committed")
    assert {key: real.get(key) for key in keys} == {
        key: fake.get(key) for key in keys
    }, (real, fake)


@pytest.mark.parametrize(
    ("skill", "commit", "retried_status"),
    [
        ("demo", {"mode": "create"}, 409),
        ("demo", {"mode": "update", "expected_live_digest": "0" * 64}, 409),
        ("fresh", {"mode": "create", "manifest": _manifest(b"something else")}, 404),
    ],
    ids=["skill_exists", "skill_bundle_conflict", "manifest_mismatch"],
)
def test_a_refused_commit_drops_the_staging(
    pair_factory, skill, commit, retried_status
):
    """B7a-tests-03: every definitive refusal before the swap drops the
    publication's staging, so its status reports nothing staged. A retried
    commit never publishes: the live-folder refusals repeat, and once past
    them there is no staging left (404)."""
    pair = pair_factory({f"{SKILL}/SKILL.md": SKILL_MD})
    _staged(pair, skill, SKILL_MD)
    params = {
        "publication_id": PUBLICATION,
        "skill_name": skill,
        "manifest": _manifest(SKILL_MD),
        **commit,
    }
    refused = pair.assert_same("fs_skill_commit", **params)
    assert refused["status"] in (400, 409)
    _publication_state(pair, skill)
    retried = pair.assert_same("fs_skill_commit", **params)
    assert retried["status"] == retried_status
    pair.assert_same_state(f"{SKILL}/SKILL.md")


def test_a_later_chunk_for_an_unstaged_file_is_not_found(pair_factory):
    pair = pair_factory()
    pair.assert_same(
        "fs_skill_stage_begin", publication_id=PUBLICATION, skill_name="demo"
    )
    real = pair.assert_same(
        "fs_skill_stage_put",
        publication_id=PUBLICATION,
        files=[{"path": "SKILL.md", "offset": 3, "data_base64": _b64(b"x")}],
    )
    assert real["status"] == 404


def test_abort_drops_the_staging(pair_factory):
    pair = pair_factory()
    _staged(pair, "fresh", SKILL_MD)
    real, fake = pair.run(
        "fs_skill_abort", publication_id=PUBLICATION, skill_name="fresh"
    )
    assert real.get("aborted") is fake.get("aborted") is True
    _publication_state(pair, "fresh")


def test_retire_takes_the_whole_folder_out(pair_factory):
    pair = pair_factory({f"{SKILL}/SKILL.md": SKILL_MD, f"{SKILL}/refs/a.md": b"a"})
    pair.assert_same("fs_skill_retire", skill_name="demo")
    assert not (pair.root / SKILL).exists()
    assert not any(path.startswith(f"{SKILL}/") for path in pair.fake.files)
    assert SKILL not in pair.fake.dirs
    real = pair.assert_same("fs_skill_retire", skill_name="demo")
    assert real["status"] == 404


# -- tree ------------------------------------------------------------------------


def _tree_shape(node: dict) -> tuple:
    return (
        node["name"],
        node["path"],
        node["type"],
        node["size"],
        node.get("file_kind"),
        tuple(_tree_shape(child) for child in node.get("children", [])),
    )


def test_tree_has_the_helpers_recursive_shape(pair_factory):
    pair = pair_factory(
        {
            "outputs/report.md": b"# Report\n",
            "outputs/data/rows.csv": b"a,b\n1,2\n",
            "outputs/data/deep/one/two/three/four/far.txt": b"far",
            "outputs/Zeta.txt": b"z",
            "outputs/.hidden.txt": b"secret",
            "outputs/node_modules/pkg.js": b"x",
            "outputs/.env": b"TOKEN=1",
        },
        dirs=["outputs/empty"],
    )
    for subfolder in ("outputs", "outputs/data"):
        real, fake = pair.run("fs_tree", subfolder=subfolder)
        assert _tree_shape(real) == _tree_shape(fake), (real, fake)
        assert real.get("root") == fake.get("root")
    real, fake = pair.run("fs_tree", subfolder="outputs/none")
    assert _outcome(real) == _outcome(fake)
