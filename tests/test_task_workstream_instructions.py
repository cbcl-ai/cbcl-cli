"""WP3: Workstream Instructions reach task sessions and the Planner directly.

Task sessions (execute, review, triage and their resumed attempts) render the
saved instructions from the session-start feed; the Planner renders them from
the synced workstream config in every mode. The executor Profile's switch
decides whether a task session receives them at all, and review and triage
follow the executor's pinned value. A feed from an older backend carries no
text, so the prompt keeps its Read pointer.
"""

from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock

import pytest

from src.config_sync.claude_md_templates._workstream import (
    _PRECEDENCE_CORE,
    TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS,
    WORKSTREAM_INSTRUCTIONS_NONE_SAVED,
    WORKSTREAM_INSTRUCTIONS_NONE_SAVED_TASK,
    WORKSTREAM_INSTRUCTIONS_OFF_LINE,
    WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE,
    _unclosed_fence,
    cut_task_workstream_instructions,
    profiles_without_workstream_instructions,
    render_task_workstream_instructions,
    render_workstream_instructions,
)
from src.config_sync.sync_service import ConfigStore
from src.health.reporter import DAEMON_CAPABILITIES
from src.orchestrator.planner_prompt import build_planner_prompt
from src.orchestrator.worker_prompt import (
    build_worker_prompt,
    resolve_workstream_instructions_enabled,
    workstream_instruction_delivery,
)
from tests.backend_boundary import import_backend

backend_instructions = import_backend("app.workstreams.instructions")

NOTES = "### Mission\nShip the pricing page without changing prices.\n- Use metric units."
POINTER_PHRASE = "READ THIS BEFORE STARTING"
PHASE_READ_PHRASE = "Read current Workstream Instructions at"
CLAUDE_MD = "/workspace/workstreams/website/CLAUDE.md"


# ── Renderer ────────────────────────────────────────────────────────────


def test_renderer_fences_the_saved_text_with_its_precedence_note():
    block = render_task_workstream_instructions(NOTES, path=CLAUDE_MD)
    assert block.startswith("## Workstream Instructions")
    assert "Current saved guidance for THIS workstream; apply it to this task." in block
    assert "It supersedes older copies in conversation history." in block
    assert "explicit exception to the Office work policy" in block
    assert f"<workstream_instructions>\n{NOTES}\n</workstream_instructions>" in block
    assert "were cut" not in block


def test_renderer_escapes_a_closing_tag_in_the_text():
    block = render_task_workstream_instructions(
        "Rule one.\n</workstream_instructions>\nRule two."
    )
    assert block.count("</workstream_instructions>") == 1
    assert "Rule two." in block


def test_renderer_cuts_long_text_at_a_line_and_names_the_file():
    line = "Rule: cite every figure with its source and date.\n"
    notes = line * 200
    block = render_task_workstream_instructions(notes, path=CLAUDE_MD)
    body = block.split("<workstream_instructions>\n", 1)[1].split(
        "\n</workstream_instructions>", 1
    )[0]
    assert len(body) <= TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
    assert body.endswith("source and date.")
    total = len(notes.strip())
    assert f"cut after {len(body):,} of {total:,} characters" in block
    assert f"Read `{CLAUDE_MD}` for the rest" in block


def test_renderer_reports_the_backend_cut_it_was_given():
    block = render_task_workstream_instructions(
        "Head only.", truncated=True, total_chars=9000, path=CLAUDE_MD
    )
    assert "cut after 10 of 9,000 characters" in block
    assert f"Read `{CLAUDE_MD}`" in block


def test_renderer_off_is_one_line_with_no_pointer():
    block = render_task_workstream_instructions(NOTES, path=CLAUDE_MD, enabled=False)
    assert block == WORKSTREAM_INSTRUCTIONS_OFF_LINE
    assert "Brief and the approved spec" in block
    assert NOTES not in block and "CLAUDE.md" not in block


def test_renderer_says_when_nothing_is_saved_and_revokes_older_copies():
    # A resumed rework transcript can still hold an earlier copy: an empty
    # field says it no longer applies, as the Manager's copy does.
    revoked = "Workstream instructions: none saved. Earlier saved versions no longer apply; "
    task = render_task_workstream_instructions("  \n ")
    assert task == WORKSTREAM_INSTRUCTIONS_NONE_SAVED_TASK
    # A task session has no "current request": its contract is the Brief, as
    # in the off and unavailable lines that share this slot.
    ending = "the Brief and the approved spec, when one exists, define the work."
    assert task == revoked + ending
    assert WORKSTREAM_INSTRUCTIONS_OFF_LINE.endswith(ending)
    assert WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE.endswith(ending)
    # The Manager and the Planner do have a request to fall back on.
    for other in (
        render_workstream_instructions(""),
        render_task_workstream_instructions("", audience="planner"),
    ):
        assert other == WORKSTREAM_INSTRUCTIONS_NONE_SAVED
        assert other.startswith(revoked) and "current request" in other


def test_both_renderers_state_the_same_precedence_rules():
    assert _PRECEDENCE_CORE in render_workstream_instructions(NOTES)
    assert _PRECEDENCE_CORE in render_task_workstream_instructions(NOTES)
    assert _PRECEDENCE_CORE in render_task_workstream_instructions(
        NOTES, audience="planner"
    )


def _fenced(before: int, code_lines: int, fence: str = "```") -> str:
    return (
        "Rule: keep it short.\n" * before
        + f"{fence}python\n"
        + "print('an example line of code')\n" * code_lines
        + f"{fence}\n"
        + "Closing rule.\n" * 50
    )


def test_a_cut_inside_a_late_code_block_stops_before_it():
    text = _fenced(before=250, code_lines=200)
    head, truncated, _ = cut_task_workstream_instructions(text)
    assert truncated
    assert "```" not in head
    assert head.endswith("Rule: keep it short.")


def test_a_cut_inside_an_early_code_block_closes_it_within_the_limit():
    text = _fenced(before=10, code_lines=400, fence="~~~~")
    head, truncated, _ = cut_task_workstream_instructions(text)
    assert truncated
    assert len(head) <= TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
    assert head.count("~~~~") == 2 and head.endswith("\n~~~~")
    # The renderer does not re-cut the backend's head.
    block = render_task_workstream_instructions(
        head, truncated=True, total_chars=len(text)
    )
    assert f"{head}\n</workstream_instructions>" in block


def test_a_closed_code_block_is_left_alone():
    text = "```\ncode\n```\n" + "Rule.\n" * 2000
    head, truncated, _ = cut_task_workstream_instructions(text)
    assert truncated and head.startswith("```\ncode\n```\nRule.")
    assert head.endswith("Rule.")


def test_a_code_span_at_line_start_is_not_an_opening_fence():
    # CommonMark: a backtick fence's info string holds no backtick.
    text = "```pip install cubicle```\n" + "Rule.\n" * 2000
    head, truncated, _ = cut_task_workstream_instructions(text)
    assert truncated and head.endswith("Rule.")
    assert head.count("```") == 2


def _block_ending_under_the_limit(under: int) -> str:
    """An early code block with a line that ends ``under`` characters before
    the limit: the closing line needs four."""
    block = "```yaml\n" + "retries: 5\n" * 200
    last = "max_retries: 1000"
    pad = TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS - under - len(block) - len(last)
    return block + "k" * (pad - 1) + f"\n{last}\n" + "tail: 1\n" * 100 + "```\n"


def test_closing_a_block_never_keeps_half_a_line():
    for under in (1, 2, 3):
        head, truncated, _ = cut_task_workstream_instructions(
            _block_ending_under_the_limit(under)
        )
        assert truncated and len(head) <= TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
        # The last line gives way whole: sliced, it would read
        # `max_retries: 10` inside a closed block.
        assert "max_retries" not in head and head.endswith("k\n```")
    # One character more room and the whole line stays.
    head, _, _ = cut_task_workstream_instructions(_block_ending_under_the_limit(4))
    assert head.endswith("max_retries: 1000\n```")
    assert len(head) == TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS


# The usual numbered-steps shape: the block is indented with its list item.
_LIST_ITEM_BLOCK = (
    "Deployment rules.\n" * 20
    + "1. Run the release script:\n\n    ```bash\n"
    + "    ./release.sh --env staging --check\n" * 200
    + "    ```\n\n2. Verify the health endpoint.\n"
)


def test_a_block_inside_a_list_item_is_closed_at_its_indentation():
    head, truncated, _ = cut_task_workstream_instructions(_LIST_ITEM_BLOCK)
    assert truncated and len(head) <= TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
    # A closer at the left margin would end the list item and open a block.
    assert head.endswith("--check\n    ```")
    assert head.count("```") == 2 and _unclosed_fence(head) is None


# A Markdown example inside a code block: its own fence lines are content.
_FENCES_INSIDE_A_BLOCK = (
    "```markdown\n1. Step:\n\n    ```bash\n    ./run.sh\n    ```\n```\n"
    + "Rule.\n" * 2000
)


def test_a_fence_line_indented_past_its_opener_does_not_close_the_block():
    head, truncated, _ = cut_task_workstream_instructions(_FENCES_INSIDE_A_BLOCK)
    # The block ends at its own closer, so the cut adds nothing.
    assert truncated and head.endswith("Rule.") and head.count("```") == 4


# A fence line about as long as the window: the block cannot be closed
# within the cap, and trimming into the fence could empty the head.
_FENCES_TOO_LONG_TO_CLOSE = (
    "`" * 7000,
    "`" * 5999 + "\nRule one.\n" * 50,
    "Intro.\n" + "~" * 7000,
)


@pytest.mark.parametrize("text", _FENCES_TOO_LONG_TO_CLOSE)
def test_a_fence_line_too_long_to_close_stays_as_cut(text):
    head, truncated, total = cut_task_workstream_instructions(text)
    assert truncated and text.startswith(head)
    assert 0 < len(head) <= TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
    # An empty head would render as "none saved" for a field that has text.
    block = render_task_workstream_instructions(
        head, truncated=True, total_chars=total
    )
    assert "none saved" not in block and "were cut after" in block


# An early code block whose long line "``` zzz…" holds the only space to cut
# at: making room for the closing line cuts it down to "```", which closes
# the block by itself.
_HALF = TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS // 2
_TRIM_CLOSES_THE_BLOCK = (
    "```\n"
    + "x" * (_HALF - 7)
    + "\n``` "
    + "z" * (_HALF - 4)
    + "\nRule.\n" * 20
)


def test_a_trim_that_ends_the_block_adds_no_second_closer():
    head, truncated, _ = cut_task_workstream_instructions(_TRIM_CLOSES_THE_BLOCK)
    assert truncated and _unclosed_fence(head) is None
    # One more closer would open a new block around the closing tag.
    assert head.count("```") == 2 and head.endswith("x\n```")


def test_planner_names_the_profiles_with_the_switch_off():
    block = render_task_workstream_instructions(
        NOTES, audience="planner", off_profiles=["brief-only", "outreach"]
    )
    assert "(now: `brief-only`, `outreach`)" in block
    assert "list_agents" not in block
    none_off = render_task_workstream_instructions(
        NOTES, audience="planner", off_profiles=[]
    )
    assert "(none has them off now)" in none_off


def test_off_profiles_are_active_custom_profiles_with_the_switch_off():
    agents = [
        {"name": "zeta", "agent_type": "custom", "workstream_instructions_enabled": False},
        {"name": "alpha", "agent_type": "custom", "workstream_instructions_enabled": False},
        {"name": "on", "agent_type": "custom", "workstream_instructions_enabled": True},
        {"name": "legacy", "agent_type": "custom"},
        {"name": "retired", "agent_type": "custom", "is_active": False,
         "workstream_instructions_enabled": False},
        {"name": "analyst", "agent_type": "system", "workstream_instructions_enabled": False},
    ]
    assert profiles_without_workstream_instructions(agents) == ["alpha", "zeta"]


def test_planner_audience_names_the_off_switch_for_briefs():
    block = render_task_workstream_instructions(NOTES, audience="planner")
    assert "use it when planning, specifying and verifying" in block
    assert "`workstream_instructions_enabled: false` in `list_agents`" in block
    task_block = render_task_workstream_instructions(NOTES)
    assert "list_agents" not in task_block


# ── Backend parity ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "line\n" * 2000,
        "word " * 3000,
        "x" * 7000,
        "a\r\nb\r\n" * 1600,
        _fenced(before=250, code_lines=200),
        _fenced(before=10, code_lines=400, fence="~~~~"),
        "```\ncode\n```\n" + "Rule.\n" * 2000,
        "```pip install cubicle```\n" + "Rule.\n" * 2000,
        _TRIM_CLOSES_THE_BLOCK,
        *(_block_ending_under_the_limit(under) for under in (1, 2, 3, 4)),
        _LIST_ITEM_BLOCK,
        _FENCES_INSIDE_A_BLOCK,
        *_FENCES_TOO_LONG_TO_CLOSE,
    ],
)
def test_cut_matches_the_backend_feed(text):
    assert (
        cut_task_workstream_instructions(text)
        == backend_instructions.task_instructions_head(text)
    )


def test_fence_closing_cut_matches_the_backend_feed_on_generated_texts():
    # The two copies must cut the same text at the same place, or the daemon
    # would re-cut the backend's head. Seeded, so a failure reproduces.
    rng = random.Random(20260930)
    pieces = [
        "Rule: keep it short.\n", "word " * 12 + "\n", "```\n", "```python\n",
        "~~~\n", "~~~~ info `x`\n", "```inline```\n", "   ```\n", "    ```\n",
        "x" * 300 + "\n", "\n", "`` not a fence\n", "````\n", "a b c\r\n",
        "1. Step:\n\n    ```sh\n", "\t```\n", "        ```\n", "~" * 250 + "\n",
    ]
    for _ in range(400):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 400)))
        limit = rng.choice([TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS, 1000, 200])
        cut = cut_task_workstream_instructions(text, limit)
        assert cut == backend_instructions.task_instructions_head(text, limit)
        head, truncated, _ = cut
        assert len(head) <= limit
        if truncated:
            assert head
            unclosed = _unclosed_fence(head)
            if unclosed is not None:
                # Only a fence line too long to fit twice stays open.
                closer, start = unclosed
                assert start + 2 * len(closer) + 1 > limit
            # A head that fits is never cut again.
            assert cut_task_workstream_instructions(head, limit) == (
                head, False, len(head),
            )


def test_constants_match_the_backend():
    assert (
        TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
        == backend_instructions.TASK_INSTRUCTIONS_MAX_CHARS
    )
    assert backend_instructions.WORKSTREAM_INSTRUCTIONS_CAPABILITY in DAEMON_CAPABILITIES


def test_daemon_roster_marks_the_switch_like_the_backend():
    store = ConfigStore()
    store.agents = [
        {
            "name": "brief-only",
            "display_name": "Brief Only",
            "agent_type": "custom",
            "role_description": "Execution — works from the brief.",
            "model": "opus",
            "allowed_tools": ["Read"],
            "workstream_instructions_enabled": False,
        },
        {
            "name": "analyst",
            "display_name": "Analyst",
            "agent_type": "system",
            "role_description": "Research standards — reads.",
            "model": "opus",
            "allowed_tools": ["Read"],
            "workstream_instructions_enabled": False,
        },
    ]
    roster = store.get_team_roster()
    line = backend_instructions.WORKSTREAM_INSTRUCTIONS_OFF_ROSTER_LINE
    # Only the custom Profile: a system agent is always on.
    assert roster.count(line) == 1
    assert roster.index(line) > roster.index("brief-only")


# ── Session resolution ──────────────────────────────────────────────────

CUSTOM_ON = {"name": "web-builder", "agent_type": "custom", "workstream_instructions_enabled": True}
CUSTOM_OFF = {"name": "web-builder", "agent_type": "custom", "workstream_instructions_enabled": False}
CUSTOM_UNSET = {"name": "web-builder", "agent_type": "custom"}
SYSTEM = {"name": "builder", "agent_type": "system", "workstream_instructions_enabled": False}


@pytest.mark.parametrize(
    ("config", "executor_flag", "expected"),
    [
        (CUSTOM_ON, False, True),
        (CUSTOM_OFF, True, False),
        (SYSTEM, False, True),
        # A snapshot pinned before the switch existed: fall back to the feed.
        (CUSTOM_UNSET, False, False),
        (CUSTOM_UNSET, None, True),
        (None, None, True),
    ],
)
def test_execution_follows_its_own_pinned_profile(config, executor_flag, expected):
    task = {"status": "in_progress"}
    if executor_flag is not None:
        task["executor_workstream_instructions_enabled"] = executor_flag
    assert resolve_workstream_instructions_enabled(config, task) is expected


@pytest.mark.parametrize("status", ["review", "blocked"])
@pytest.mark.parametrize("executor_flag", [True, False])
def test_review_and_triage_follow_the_executor(status, executor_flag):
    # The reviewer / Manager Assistant's own Profile never decides.
    for config in (CUSTOM_ON, CUSTOM_OFF, SYSTEM):
        task = {"status": status, "executor_workstream_instructions_enabled": executor_flag}
        assert resolve_workstream_instructions_enabled(config, task) is executor_flag


def test_missing_executor_flag_means_on():
    assert resolve_workstream_instructions_enabled(CUSTOM_OFF, {"status": "review"})


def test_delivery_mode():
    assert workstream_instruction_delivery({}) == "legacy"
    # A session resolved off stays off without the feed text (the backend
    # lookup failed): the switch never fails open into the Read pointer.
    assert (
        workstream_instruction_delivery({"workstream_instructions_enabled": False})
        == "off"
    )
    assert (
        workstream_instruction_delivery({"workstream_instructions_enabled": True})
        == "legacy"
    )
    assert workstream_instruction_delivery({"workstream_instructions": ""}) == "inject"
    assert (
        workstream_instruction_delivery(
            {"workstream_instructions": NOTES, "workstream_instructions_enabled": False}
        )
        == "off"
    )
    assert (
        workstream_instruction_delivery(
            {"workstream_instructions": NOTES, "executor_workstream_instructions_enabled": False}
        )
        == "off"
    )


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        # The backend said its lookup failed. Review and triage follow the
        # executor's switch, which is then unknown: neither text nor pointer.
        ({"status": "review"}, "unavailable"),
        ({"status": "blocked"}, "unavailable"),
        # An executing session follows its own switch: on keeps the pointer,
        # off stays off.
        ({"status": "in_progress"}, "legacy"),
        ({"status": "ready"}, "legacy"),
        ({}, "legacy"),
        ({"status": "in_progress", "workstream_instructions_enabled": False}, "off"),
        ({"status": "review", "workstream_instructions_enabled": False}, "off"),
    ],
)
def test_delivery_mode_when_the_backend_lookup_failed(task, expected):
    task = {**task, "workstream_instructions_unavailable": True}
    assert workstream_instruction_delivery(task) == expected


@pytest.mark.parametrize("status", ["review", "blocked"])
def test_an_older_backend_never_reads_as_a_failed_lookup(status):
    assert workstream_instruction_delivery({"status": status}) == "legacy"
    assert (
        workstream_instruction_delivery(
            {"status": status, "workstream_instructions_unavailable": False}
        )
        == "legacy"
    )


# ── Task prompts ────────────────────────────────────────────────────────

_BASE = {
    "task_id": "33333333-3333-3333-3333-333333333333",
    "readable_id": "WB-001.T01",
    "title": "Write the about page",
    "priority": "medium",
    "assigned_agent": "builder",
    "reviewer": "auditor",
    "workstream_context": {"name": "Website"},
    "brief": {
        "goal": "Publish an about page.",
        "inputs": "Add an about page.",
        "acceptance_criteria": ["The page states the mission."],
        "verification_steps": "Execution checks: open the page.",
    },
    "recent_activities": [],
}

SESSIONS = {
    "execute": {"status": "in_progress"},
    "rework_resume": {
        "status": "in_progress",
        "prior_session_id": "session-1",
        "rework_feedback": "Add the mission statement.",
        "rework_count": 1,
    },
    "review": {"status": "review"},
    "triage": {"status": "blocked", "assigned_agent": "builder"},
}


def _feed(enabled: bool, notes: str = NOTES) -> dict:
    return {
        "workstream_instructions": notes,
        "workstream_instructions_truncated": False,
        "workstream_instructions_chars": len(notes),
        "executor_workstream_instructions_enabled": enabled,
        "workstream_instructions_enabled": enabled,
    }


def _no_pointer(prompt: str) -> None:
    assert POINTER_PHRASE not in prompt
    assert PHASE_READ_PHRASE not in prompt
    assert "STEP 0.0" not in prompt


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_sessions_receive_the_block_when_on(session):
    prompt = build_worker_prompt({**_BASE, **SESSIONS[session], **_feed(True)})
    assert prompt.count("## Workstream Instructions") == 1
    assert prompt.count(NOTES) == 1
    assert WORKSTREAM_INSTRUCTIONS_OFF_LINE not in prompt
    _no_pointer(prompt)


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_sessions_receive_nothing_but_one_line_when_off(session):
    prompt = build_worker_prompt({**_BASE, **SESSIONS[session], **_feed(False)})
    assert NOTES not in prompt
    assert "## Workstream Instructions" not in prompt
    assert prompt.count(WORKSTREAM_INSTRUCTIONS_OFF_LINE) == 1
    _no_pointer(prompt)


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_cleared_instructions_revoke_older_copies(session):
    # A resumed rework transcript still holds the block it was given before
    # the user cleared the field: the empty case must say it no longer applies.
    prompt = build_worker_prompt({**_BASE, **SESSIONS[session], **_feed(True, notes="")})
    assert prompt.count(WORKSTREAM_INSTRUCTIONS_NONE_SAVED_TASK) == 1
    assert "current request" not in prompt
    assert "## Workstream Instructions" not in prompt
    _no_pointer(prompt)


@pytest.mark.parametrize("session", ["review", "triage"])
def test_a_failed_lookup_renders_one_honest_line_and_no_pointer(session):
    prompt = build_worker_prompt(
        {**_BASE, **SESSIONS[session], "workstream_instructions_unavailable": True}
    )
    assert prompt.count(WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE) == 1
    assert WORKSTREAM_INSTRUCTIONS_OFF_LINE not in prompt
    assert "## Workstream Instructions" not in prompt
    _no_pointer(prompt)


def test_off_keeps_the_brief_and_workstream_name():
    prompt = build_worker_prompt({**_BASE, **SESSIONS["execute"], **_feed(False)})
    assert "# Workstream: Website" in prompt
    assert "Publish an about page." in prompt


def test_a_long_feed_names_the_workstream_file():
    notes = "Rule.\n" * 1000
    head = cut_task_workstream_instructions(notes)[0]
    prompt = build_worker_prompt(
        {
            **_BASE,
            **SESSIONS["execute"],
            "workstream_instructions": head,
            "workstream_instructions_truncated": True,
            "workstream_instructions_chars": len(notes.strip()),
            "workstream_instructions_enabled": True,
        }
    )
    assert f"Read `{CLAUDE_MD}` for the rest" in prompt


def test_an_older_backend_keeps_the_read_pointer():
    execute = build_worker_prompt({**_BASE, **SESSIONS["execute"]})
    assert POINTER_PHRASE in execute and CLAUDE_MD in execute
    assert "## Workstream Instructions" not in execute
    review = build_worker_prompt({**_BASE, **SESSIONS["review"]})
    assert PHASE_READ_PHRASE in review


# ── Planner ─────────────────────────────────────────────────────────────

PLANNER_MODES = ("specify", "scope_plan", "materialize", "research", "verify")


def _consult(mode: str, ws_ctx: dict) -> dict:
    consult = {"workstream_id": "ws-1", "mode": mode, "objective": "Plan release"}
    if mode != "specify":
        consult["scope_id"] = "scope-1"
    return {"task_id": "planner-0123456789ab", "planner_consult": consult, "workstream_context": ws_ctx}


@pytest.mark.parametrize("mode", PLANNER_MODES)
def test_every_planner_mode_receives_the_instructions(mode):
    text = build_planner_prompt(_consult(mode, {"name": "Website", "instructions": NOTES}))
    assert text.count(NOTES) == 1
    assert "use it when planning, specifying and verifying" in text
    assert "Before planning, Read" not in text


@pytest.mark.parametrize("mode", PLANNER_MODES)
def test_a_planner_row_without_instructions_keeps_the_pointer(mode):
    text = build_planner_prompt(_consult(mode, {"name": "Website"}))
    assert f"Before planning, Read `{CLAUDE_MD}`" in text
    assert "## Workstream Instructions" not in text


def test_planner_with_nothing_saved_says_so():
    text = build_planner_prompt(_consult("specify", {"name": "Website", "instructions": ""}))
    assert WORKSTREAM_INSTRUCTIONS_NONE_SAVED in text


def test_planner_prompt_names_the_synced_off_profiles():
    ws_ctx = {"name": "Website", "instructions": NOTES}
    named = build_planner_prompt(
        _consult("materialize", {**ws_ctx, "instructions_off_profiles": ["brief-only"]})
    )
    assert "(now: `brief-only`)" in named
    # A consult marker without the list (an older queue entry) names the field.
    fallback = build_planner_prompt(_consult("materialize", ws_ctx))
    assert "`workstream_instructions_enabled: false` in `list_agents`" in fallback


def test_list_agents_contract_says_what_the_switch_means():
    # The Planner's fallback sentence and the Manager's roster line both rely
    # on the field `list_agents` returns: its description must explain it.
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_planner import get_planner_tools

    for tools in (get_manager_tools(), get_planner_tools()):
        description = next(t for t in tools if t["name"] == "list_agents")["description"]
        assert "`workstream_instructions_enabled: false`" in description
        assert "never receive the workstream's saved instructions" in description


async def test_consult_handler_reads_the_off_profiles_from_the_synced_roster():
    from src.handlers import _BACKGROUND_TASKS, _planner_cap_cooldown, _planner_consults
    from tests.test_review_circuit_breaker import build_harness

    h = await build_harness()
    h.supervisor.spawn_worker = AsyncMock(return_value=True)
    h.config_store.get_agent.return_value = {"name": "planner"}
    h.config_store.get_workstream.return_value = {
        "name": "Website",
        "context_notes": NOTES,
    }
    h.config_store.agents = [
        {"name": "brief-only", "agent_type": "custom", "workstream_instructions_enabled": False},
        {"name": "web-builder", "agent_type": "custom", "workstream_instructions_enabled": True},
        {"name": "analyst", "agent_type": "system", "workstream_instructions_enabled": False},
    ]
    handler = {call.args[0]: call.args[1] for call in h.router.on.call_args_list}[
        "consult_planner"
    ]
    try:
        await handler(
            {
                "mode": "materialize",
                "objective": "Author the briefs",
                "workstream_id": "ws-1",
                "scope_id": "scope-1",
            }
        )
        _agent, _config, task_data = h.supervisor.spawn_worker.call_args.args
        assert task_data["workstream_context"]["instructions"] == NOTES
        assert task_data["workstream_context"]["instructions_off_profiles"] == [
            "brief-only"
        ]
        assert "(now: `brief-only`)" in build_planner_prompt(task_data)
    finally:
        for background in list(_BACKGROUND_TASKS):
            if not background.done():
                background.cancel()
        await asyncio.sleep(0)
        _planner_consults.clear()
        _planner_cap_cooldown.clear()

