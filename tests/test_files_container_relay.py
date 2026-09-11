"""Container selection and cancellation tests without a Docker service."""

import asyncio
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import AsyncMock

import pytest

from src import fs_handler as relay
from src._agent_image import secure_files

CONTAINER = "a" * 64
OFFICE = str(uuid.UUID(int=1))


def inspection(**updates):
    container = {
        "Id": CONTAINER,
        "State": {"Running": True},
        "Config": {"Labels": {"cbcl.office_id": OFFICE}},
        "Mounts": [{"Destination": "/workspace", "Source": "/synthetic/office"}],
    }
    container.update(updates)
    return 0, json.dumps([container]).encode()


@pytest.mark.asyncio
async def test_launch_is_fixed_isolated_and_uses_only_selected_container(monkeypatch):
    process = AsyncMock(side_effect=[inspection(), (0, b'{"size":3}')])
    monkeypatch.setattr(relay, "_run_process", process)
    sender = AsyncMock()
    handler = relay.FsHandler(CONTAINER, office_id=OFFICE)
    await handler.handle_request(
        {
            "request_id": "backend-id",
            "action": "fs_read",
            "params": {
                "path": "name;$(evil).txt",
                "container_id": "foreign",
                "root": "/home/agent",
            },
        },
        sender,
    )
    arguments, payload = process.await_args_list[1].args
    assert arguments[:8] == [
        "docker",
        "exec",
        "-i",
        "--user",
        "1000:1000",
        "--workdir",
        "/",
        CONTAINER,
    ]
    assert arguments[8:13] == [
        "/usr/local/bin/python3",
        "-I",
        "-S",
        relay.HELPER_PATH,
        "--request-id",
    ]
    assert len(arguments[13]) == 32
    assert "name;$(evil).txt" not in arguments
    assert json.loads(payload)["params"]["path"] == "name;$(evil).txt"
    assert sender.await_args.args[0] == {
        "type": "response",
        "request_id": "backend-id",
        "data": {"size": 3},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["", "office-name", "--privileged", "b" * 12])
async def test_missing_or_named_container_never_executes_or_falls_back(
    monkeypatch, container
):
    process = AsyncMock()
    monkeypatch.setattr(relay, "_run_process", process)
    sender = AsyncMock()
    await relay.FsHandler(container, office_id=OFFICE).handle_request(
        {"action": "fs_download_zip", "params": {}}, sender
    )
    assert sender.await_args.args[0]["data"]["status"] == 503
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_container_is_resolved_once_and_cleanup_uses_that_identity(
    monkeypatch,
):
    replacement = "b" * 64
    resolver = AsyncMock(return_value=replacement)
    process = AsyncMock(
        side_effect=[inspection(Id=replacement), TimeoutError(), (0, b"")]
    )
    monkeypatch.setattr(relay, "_run_process", process)
    sender = AsyncMock()
    handler = relay.FsHandler(CONTAINER, office_id=OFFICE, container_resolver=resolver)
    await handler.handle_request({"action": "fs_download_zip", "params": {}}, sender)
    resolver.assert_awaited_once_with()
    assert process.await_args_list[0].args[0][-1] == replacement
    assert process.await_args_list[1].args[0][7] == replacement
    assert process.await_args_list[2].args[0][7] == replacement
    assert sender.await_args.args[0]["data"]["status"] == 408


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"Id": "b" * 64},
        {"State": {"Running": False}},
        {"Config": {"Labels": {"cbcl.office_id": str(uuid.UUID(int=2))}}},
        {"Mounts": []},
    ],
)
async def test_stopped_replaced_or_foreign_container_never_executes(
    monkeypatch, changed
):
    process = AsyncMock(return_value=inspection(**changed))
    monkeypatch.setattr(relay, "_run_process", process)
    sender = AsyncMock()
    await relay.FsHandler(CONTAINER, office_id=OFFICE).handle_request(
        {"action": "fs_read", "params": {"path": "public.txt"}}, sender
    )
    assert process.await_count == 1
    assert sender.await_args.args[0]["data"]["status"] == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), RuntimeError("missing helper"), ValueError("oversized output")],
)
async def test_failed_export_cancels_exact_helper_and_returns_no_archive(
    monkeypatch, failure
):
    process = AsyncMock(side_effect=[inspection(), failure, (0, b"")])
    monkeypatch.setattr(relay, "_run_process", process)
    sender = AsyncMock()
    await relay.FsHandler(CONTAINER, office_id=OFFICE).handle_request(
        {"action": "fs_download_zip", "params": {"path": "outputs"}}, sender
    )
    launched = process.await_args_list[1].args[0]
    canceled = process.await_args_list[2].args[0]
    assert canceled[-2:] == ["--cancel", launched[-1]]
    assert canceled[7] == CONTAINER
    assert sender.await_args.args[0]["data"]["status"] in {408, 503}
    assert "content_base64" not in sender.await_args.args[0]["data"]
    assert CONTAINER not in relay._ACTIVE_CONTAINERS


@pytest.mark.asyncio
async def test_disconnect_cancels_helper_before_releasing_concurrency_slot(monkeypatch):
    started = asyncio.Event()
    calls = []

    async def process(arguments, payload, **options):
        calls.append(arguments)
        if arguments[1] == "inspect":
            return inspection()
        if arguments[-2] == "--cancel":
            assert CONTAINER in relay._ACTIVE_CONTAINERS
            return 0, b""
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(relay, "_run_process", process)
    handler = relay.FsHandler(CONTAINER, office_id=OFFICE)
    task = asyncio.create_task(
        handler._dispatch("fs_download_zip", {"path": "outputs"})
    )
    await started.wait()
    assert (await handler._dispatch("fs_read", {"path": "public.txt"}))["status"] == 429
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-1][-2:] == ["--cancel", calls[-2][-1]]
    assert CONTAINER not in relay._ACTIVE_CONTAINERS


@pytest.mark.asyncio
async def test_input_output_caps_and_host_client_timeout(monkeypatch):
    process = AsyncMock()
    monkeypatch.setattr(relay, "_run_process", process)
    monkeypatch.setattr(relay, "MAX_REQUEST_BYTES", 4)
    result = await relay.FsHandler(CONTAINER, office_id=OFFICE)._dispatch(
        "fs_write", {"path": "public.txt", "content": "large"}
    )
    assert result["status"] == 413
    process.assert_not_awaited()
    stream = asyncio.StreamReader()
    stream.feed_data(b"too much")
    stream.feed_eof()
    with pytest.raises(ValueError, match="exceeds"):
        await relay._bounded_read(stream, 4)


@pytest.mark.asyncio
async def test_cancel_mode_terminates_only_matching_real_synthetic_helper(
    tmp_path, monkeypatch
):
    program = tmp_path / "synthetic_helper.py"
    program.write_text(
        "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready',flush=True)\ntime.sleep(60)\n"
    )
    monkeypatch.setattr(secure_files, "HELPER_PATH", str(program))
    marker = uuid.uuid4().hex
    target = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        str(program),
        "--request-id",
        marker,
        stdout=asyncio.subprocess.PIPE,
    )
    unrelated = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        str(program),
        "--request-id",
        uuid.uuid4().hex,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert await target.stdout.readline() == b"ready\n"
        assert await unrelated.stdout.readline() == b"ready\n"
        await asyncio.to_thread(secure_files.cancel_request, marker)
        await asyncio.wait_for(target.wait(), timeout=2)
        assert target.returncode is not None
        assert unrelated.returncode is None
    finally:
        for process in [target, unrelated]:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_isolated_interpreter_ignores_workspace_modules_and_startup_hooks(
    tmp_path,
):
    marker = tmp_path / "unexpected-execution"
    for name in ["json.py", "zipfile.py", "sitecustomize.py", "usercustomize.py"]:
        (tmp_path / name).write_text(f"raise RuntimeError({str(marker)!r})\n")
    script = Path(secure_files.__file__)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        str(script),
        "--request-id",
        uuid.uuid4().hex,
        cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await process.communicate(b"not-json")
    assert process.returncode == 0, errors.decode()
    assert json.loads(output)["status"] == 400
    assert not marker.exists()


@pytest.mark.asyncio
async def test_helper_self_deadline_ends_disconnected_stdin_read(tmp_path):
    runner = tmp_path / "deadline_helper.py"
    runner.write_text(
        "import importlib.util,sys\n"
        f"spec=importlib.util.spec_from_file_location('secure_files',{secure_files.__file__!r})\n"
        "helper=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(helper)\n"
        "helper.DEADLINE_SECONDS=1\nraise SystemExit(helper.main())\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        str(runner),
        "--request-id",
        uuid.uuid4().hex,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=4)
        assert process.returncode == 0
        assert json.loads(await process.stdout.read())["status"] == 400
    finally:
        process.stdin.close()
        if process.returncode is None:
            process.kill()
        await process.wait()
