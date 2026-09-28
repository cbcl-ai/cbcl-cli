"""X12/X29 on the daemon: generation never replaces an existing SKILL.md.

A current backend fixes the slug, checks the catalog and the Office, and
asks the daemon NOT to write (``defer_write``); it then writes with an
exclusive create. ``create_only`` makes any daemon-side write exclusive:
the revision helper's ``fs_write_revision(expect_absent)`` refuses an
existing file atomically, and an office image that predates it (426) is
read first and written only after a definitive 404.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src._agent_image.secure_files import execute
from src._handlers._requests import dispatch_backend_request
from src._setup_skill_io import SkillAlreadyExistsError, write_skill_to_workspace

REL = ".claude/skills/code-review/SKILL.md"


@pytest.fixture
def helper(tmp_path):
    calls: list[str] = []

    async def dispatch(action, params):
        calls.append(action)
        return execute({"action": action, "params": params}, tmp_path)

    return SimpleNamespace(_dispatch=dispatch, calls=calls, root=tmp_path)


@pytest.fixture
def old_helper(tmp_path):
    """An office image without the revision actions."""
    calls: list[str] = []

    async def dispatch(action, params):
        calls.append(action)
        if action in {"fs_hash", "fs_write_revision"}:
            return {
                "error": "Unknown or invalid filesystem request",
                "status": 426,
                "code": "office_image_upgrade_required",
            }
        return execute({"action": action, "params": params}, tmp_path)

    return SimpleNamespace(_dispatch=dispatch, calls=calls, root=tmp_path)


async def test_create_only_writes_a_new_skill(helper):
    rel = await write_skill_to_workspace(
        helper, {"playbook_content": "# new\n"}, "code-review", create_only=True
    )
    assert rel == REL
    assert (helper.root / REL).read_text() == "# new\n"
    assert helper.calls == ["fs_write_revision"]


async def test_create_only_refuses_an_existing_skill(helper):
    target = helper.root / REL
    target.parent.mkdir(parents=True)
    target.write_text("# the user's playbook\n")
    with pytest.raises(SkillAlreadyExistsError):
        await write_skill_to_workspace(
            helper, {"playbook_content": "# model\n"}, "code-review", create_only=True
        )
    assert target.read_text() == "# the user's playbook\n"


async def test_create_only_on_old_image_reads_then_writes(old_helper):
    await write_skill_to_workspace(
        old_helper, {"playbook_content": "# new\n"}, "code-review", create_only=True
    )
    assert old_helper.calls == ["fs_write_revision", "fs_read", "fs_write"]
    assert (old_helper.root / REL).read_text() == "# new\n"


async def test_create_only_on_old_image_never_overwrites(old_helper):
    target = old_helper.root / REL
    target.parent.mkdir(parents=True)
    target.write_text("# keep\n")
    with pytest.raises(SkillAlreadyExistsError):
        await write_skill_to_workspace(
            old_helper,
            {"playbook_content": "# model\n"},
            "code-review",
            create_only=True,
        )
    assert target.read_text() == "# keep\n"
    assert "fs_write" not in old_helper.calls


async def test_legacy_callers_keep_replace_semantics(helper):
    target = helper.root / REL
    target.parent.mkdir(parents=True)
    target.write_text("# old\n")
    await write_skill_to_workspace(
        helper, {"playbook_content": "# v2\n"}, "code-review"
    )
    assert target.read_text() == "# v2\n"


def _harness(monkeypatch, generated: dict):
    sent: list[dict] = []

    async def _send(frame: dict) -> None:
        sent.append(frame)

    router = SimpleNamespace(ws_client=SimpleNamespace(send=_send))
    office = SimpleNamespace(id="office-1", workspace_path="synthetic-workspace")
    monkeypatch.setattr(
        "src.office_runtime.resolve_office_container_id",
        AsyncMock(return_value="a" * 64),
    )

    async def _fake_generator(*args, **kwargs):
        return dict(generated)

    import src.setup_generator as sg

    monkeypatch.setattr(sg, "generate_skill_from_overview", _fake_generator)
    return router, office, sent


async def _request(router, office, fs_handler, params):
    await dispatch_backend_request(
        {"request_id": "r1", "action": "generate_skill", "params": params},
        router=router,
        fs_handler=fs_handler,
        office=office,
        redis_client=None,
        container_name="cbcl-office-acme",
    )


async def test_defer_write_returns_content_without_writing(monkeypatch, helper):
    router, office, sent = _harness(
        monkeypatch, {"name": "x", "playbook_content": "# body\n"}
    )
    await _request(
        router,
        office,
        helper,
        {
            "overview": "An overview.",
            "name": "code-review",
            "defer_write": True,
            "create_only": True,
        },
    )
    data = sent[0]["data"]
    assert data["write_deferred"] is True
    assert data["resolved_path"] == REL
    assert "written_path" not in data
    assert helper.calls == []
    assert not (helper.root / REL).exists()


async def test_create_only_collision_is_a_409_error(monkeypatch, helper):
    target = helper.root / REL
    target.parent.mkdir(parents=True)
    target.write_text("# keep\n")
    router, office, sent = _harness(
        monkeypatch, {"name": "x", "playbook_content": "# body\n"}
    )
    await _request(
        router,
        office,
        helper,
        {"overview": "An overview.", "name": "code-review", "create_only": True},
    )
    data = sent[0]["data"]
    assert data["status"] == 409 and data["code"] == "skill_exists"
    assert target.read_text() == "# keep\n"
