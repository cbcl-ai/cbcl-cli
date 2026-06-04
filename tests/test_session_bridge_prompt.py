"""Regression test for the dash-leading-prompt CLI bug (2026-06-04).

A user pasted a chat message whose first line was a markdown bullet
(``- SMTP_CREDENTIALS …``). The Manager turn passed that text to
``claude --print`` as a POSITIONAL argument, and the CLI's commander
parser saw the leading ``-`` and rejected it::

    Claude CLI exited with code 1
    error: unknown option '- SMTP_CREDENTIALS is in the Office secrets …'

The fix: terminate option parsing with ``--`` before the positional
prompt, so anything starting with ``-`` is taken as the prompt rather
than a flag. This test pins that the command builder emits
``[…, "--", <prompt>]`` (and never ``["-p", <prompt>]``).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.docker import session_bridge


class _Captured(RuntimeError):
    """Raised by the create_subprocess_exec mock to short-circuit the
    generator right after the command is built — we only care about the
    argv, not about actually streaming a CLI session."""


async def _build_cmd(prompt: str, **kwargs) -> list[str]:
    """Drive ``stream_cli_session`` far enough to capture the argv it
    would have passed to ``asyncio.create_subprocess_exec``.

    ``system_prompt=""`` makes the generator skip the ``tee`` prompt-file
    path, so the FIRST (and only) ``create_subprocess_exec`` reached is
    the main ``claude --print`` invocation.
    """
    captured: list[list[str]] = []

    async def _fake_exec(*args, **_kw):
        captured.append(list(args))
        raise _Captured()

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec):
        agen = session_bridge.stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-opus-4-7",
            system_prompt="",  # skip the tee path
            prompt=prompt,
            **kwargs,
        )
        # The generator may swallow the exception and yield an error
        # SessionMessage; either way ``captured`` is populated.
        try:
            async for _ in agen:
                break
        except _Captured:
            pass

    assert captured, "create_subprocess_exec was never reached"
    return captured[0]


@pytest.mark.asyncio
async def test_prompt_is_positional_after_double_dash():
    cmd = await _build_cmd("hello world")
    # The prompt is the final argv element, immediately preceded by ``--``.
    assert cmd[-1] == "hello world"
    assert cmd[-2] == "--"


@pytest.mark.asyncio
async def test_dash_leading_prompt_not_parsed_as_option():
    dash_prompt = "- SMTP_CREDENTIALS is in the Office secrets\n\nSkip Sentry"
    cmd = await _build_cmd(dash_prompt)
    assert cmd[-1] == dash_prompt
    assert cmd[-2] == "--"
    # The old, broken form passed ``["-p", prompt]`` — make sure the prompt
    # is never the value of a ``-p`` flag again.
    for i, tok in enumerate(cmd[:-1]):
        if tok == "-p":
            assert cmd[i + 1] != dash_prompt, "prompt still passed via -p"


@pytest.mark.asyncio
async def test_print_mode_still_enabled():
    # ``--print`` (not the redundant ``-p``) carries print mode.
    cmd = await _build_cmd("hi")
    assert "--print" in cmd
