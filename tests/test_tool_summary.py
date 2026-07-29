"""Unit tests for tool-call activity enrichment (``src/_tool_summary.py``).

Covers per-tool input summaries, output previews, secret redaction (the
host-side guarantee that no secret reaches the platform DB), and the
secret-file output skip.
"""
from __future__ import annotations

from src._tool_summary import (
    build_tool_activity,
    is_secret_file_read,
    output_preview,
    redact_secrets,
    summarize_tool_call,
)


def test_bash_summary_uses_command() -> None:
    label, preview = summarize_tool_call("Bash", {"command": "npm run build"})
    assert label == "Bash"
    assert preview == "npm run build"


def test_read_summary_uses_file_path() -> None:
    label, preview = summarize_tool_call("Read", {"file_path": "/workspace/app.py"})
    assert label == "Read"
    assert preview == "/workspace/app.py"


def test_grep_summary_includes_path() -> None:
    _, preview = summarize_tool_call("Grep", {"pattern": "foo", "path": "src/"})
    assert "foo" in preview and "src/" in preview


def test_websearch_and_webfetch() -> None:
    assert summarize_tool_call("WebSearch", {"query": "python"})[1] == "python"
    assert summarize_tool_call("WebFetch", {"url": "https://x.com"})[1] == (
        "https://x.com"
    )


def test_mcp_namespace_is_stripped() -> None:
    label, _ = summarize_tool_call("mcp__cubicle-tools__get_board", {})
    assert label == "get_board"


def test_unknown_tool_falls_back_to_first_string() -> None:
    label, preview = summarize_tool_call("SomeTool", {"thing": "value-here"})
    assert label == "SomeTool"
    assert preview == "value-here"


def test_redaction_scrubs_common_token_shapes() -> None:
    assert "sk-ant-" not in redact_secrets("key sk-ant-abcdefghijklmnopqrstuvwx")
    assert "ghp_" not in redact_secrets("ghp_0123456789abcdefghijABCDEF→")
    assert "AKIA" not in redact_secrets("cred AKIAIOSFODNN7EXAMPLE done")
    assert "«redacted»" in redact_secrets("Bearer abcdefghijklmnopqrstuvwxyz")


def test_redaction_keeps_key_masks_value() -> None:
    out = redact_secrets('API_KEY="supersecretvalue123"')
    assert "API_KEY" in out
    assert "supersecretvalue123" not in out


def test_redaction_applies_to_summary() -> None:
    _, preview = summarize_tool_call(
        "Bash", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrst'"}
    )
    assert "abcdefghijklmnopqrst" not in preview


def test_output_preview_truncates() -> None:
    long = "x" * 2000
    out = output_preview(long)
    assert len(out) < 700
    assert "truncated" in out


def test_output_preview_flattens_block_list() -> None:
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert "hello" in output_preview(content)
    assert "world" in output_preview(content)


def test_secret_file_read_detected() -> None:
    assert is_secret_file_read("Read", {"file_path": "/ws/.scripts/x/.secrets.json"})
    assert is_secret_file_read("Read", {"file_path": "/a/.secrets/b"})
    assert not is_secret_file_read("Read", {"file_path": "/ws/app.py"})
    assert not is_secret_file_read("Bash", {"command": "cat .secrets.json"})


def test_build_activity_with_result() -> None:
    out = build_tool_activity(
        "Bash",
        {"command": "echo hi"},
        result_content="hi\n",
        is_error=False,
    )
    assert out["content"] == "Bash: echo hi"
    assert out["details"]["tool"] == "Bash"
    assert out["details"]["summary"] == "echo hi"
    assert out["details"]["output_preview"] == "hi"
    assert "is_error" not in out["details"]


def test_build_activity_error_flag() -> None:
    out = build_tool_activity(
        "Bash", {"command": "false"}, result_content="boom", is_error=True
    )
    assert out["details"]["is_error"] is True


def test_build_activity_skips_secret_file_output() -> None:
    out = build_tool_activity(
        "Read",
        {"file_path": "/ws/.scripts/x/.secrets.json"},
        result_content='{"API_KEY": "leak"}',
    )
    assert "output_preview" not in out["details"]


def test_build_activity_input_only() -> None:
    out = build_tool_activity("Glob", {"pattern": "**/*.py"})
    assert out["content"] == "Glob: **/*.py"
    assert "output_preview" not in out["details"]


def test_running_start_row() -> None:
    """The 'running' start row (emitted on tool_use) carries the command +
    tool_use_id + running flag, and NO output yet."""
    out = build_tool_activity(
        "Bash", {"command": "npm run build"}, tool_use_id="tu_1", running=True
    )
    assert out["content"] == "Bash: npm run build"
    assert out["details"]["tool_use_id"] == "tu_1"
    assert out["details"]["running"] is True
    assert "output_preview" not in out["details"]


def test_end_row_carries_tool_use_id_and_output() -> None:
    """The 'end' row (emitted on tool_result) shares the tool_use_id and adds
    output; it is NOT marked running."""
    out = build_tool_activity(
        "Bash",
        {"command": "npm run build"},
        result_content="compiled ok",
        tool_use_id="tu_1",
    )
    assert out["details"]["tool_use_id"] == "tu_1"
    assert out["details"]["output_preview"] == "compiled ok"
    assert "running" not in out["details"]


def test_end_row_carries_duration_ms() -> None:
    """Manager-feed parity: when the caller timed the start→end pair,
    ``duration_ms`` rides the end row's details (int-coerced)."""
    out = build_tool_activity(
        "Bash",
        {"command": "sleep 2"},
        result_content="",
        tool_use_id="tu_1",
        duration_ms=2041.7,
    )
    assert out["details"]["duration_ms"] == 2041
    # Absent unless explicitly passed — 0 is a legitimate duration.
    assert "duration_ms" not in build_tool_activity(
        "Bash", {"command": "ls"}, tool_use_id="tu_2", running=True
    )["details"]
    assert build_tool_activity(
        "Bash", {"command": "ls"}, tool_use_id="tu_3", duration_ms=0
    )["details"]["duration_ms"] == 0


def test_sidechain_rows_carry_marker_and_parent_id() -> None:
    """Rows originating inside a dynamic-workflow subagent carry
    ``sidechain: True`` + the spawning block's ``parent_tool_use_id`` so
    the Console nests them under the Agent/Task spawn row."""
    out = build_tool_activity(
        "Bash",
        {"command": "pytest -q"},
        tool_use_id="tu_9",
        running=True,
        sidechain=True,
        parent_tool_use_id="spawn-1",
    )
    assert out["details"]["sidechain"] is True
    assert out["details"]["parent_tool_use_id"] == "spawn-1"
    # Parent-stream rows stay unmarked (no noise keys on the common case).
    plain = build_tool_activity("Bash", {"command": "ls"}, tool_use_id="tu_1")
    assert "sidechain" not in plain["details"]
    assert "parent_tool_use_id" not in plain["details"]
    # parent_tool_use_id is only meaningful WITH the sidechain flag.
    no_flag = build_tool_activity(
        "Bash", {"command": "ls"}, parent_tool_use_id="spawn-1"
    )
    assert "parent_tool_use_id" not in no_flag["details"]
