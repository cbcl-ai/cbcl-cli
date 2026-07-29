"""Eval family: pivot-2 pins (branch pivot-2/phase-1).

Phase-1 pins — the choice-selector primitive's prompt-surface facts
(docs/pivot_2/plan/03-implementation-plan.md, P1-2/P1-3/P1-5):

* the ``ask_user_choice`` tool schema (2-4 options, key pattern, kinds
  enum, the end-turn + never-poll + anti-nag sentences),
* the General-Chat registration strip (no selector may fire there),
* the Manager-only catalog placement (no worker/Planner surface),
* the session PRE-LOCK after a successful ask (D2 — asking ENDS the turn),
* the supersession context line (D3 — typing always wins),
* the transform's context_key injection.

Phase-2 pins (P2-3 — the Manager playbook boundary flow):

* the program-boundary ask block (exact big_assignment/program option copy,
  end-the-turn instruction),
* the D6 anti-nag hard rules (silent classification; explicit wording =
  one-click confirmation unless the program is already consented — C-1;
  informational asks scoped in — C-5; never re-ask; one open question),
* the D1 never-flip-yourself rule (consent is backend-applied from the
  user's own click),
* teaching errors as the cue to ask (never surfaced to the user),
* the workstream mental-model line,
* NEGATIVE: the retired dial-flip copy (settings / `work_mode`) appears
  nowhere in the playbook or the default-mode context banner.

Phase-3 pins (P3-1 — option C, a program in its own workstream):

* the `proposed_workstream_name` schema (bounds; required-with-own_workstream
  and backend-creates-from-the-click semantics in the descriptions),
* the playbook's option-C inclusion conditions (live program in this
  workstream, or a clearly separate vector),
* the exact option-C copy template (key/label/description + the
  proposed-name guidance),
* the post-click do-nothing rule (the hand-off chip is the answer; the
  program continues in the new workstream),
* D5 still holds: the Manager is never instructed to create workstreams —
  the backend creates them from the user's click only.

Same discipline as the pivot-1 family: each assertion targets a specific
load-bearing sentence or schema fact; deleting or paraphrasing it away
fails the eval.
"""
from __future__ import annotations

import asyncio

from src._agent_image import mcp_tool_server as mts
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.transforms import transform_params
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD

_MANAGER_NORM = " ".join(MANAGER_CLAUDE_MD.split())


def _ask_tool() -> dict:
    for tool in get_manager_tools():
        if tool["name"] == "ask_user_choice":
            return tool
    raise AssertionError("ask_user_choice not found in the Manager catalog")


# ---------------------------------------------------------------------------
# P1-2 — tool schema pin
# ---------------------------------------------------------------------------


def test_ask_user_choice_schema_shape() -> None:
    tool = _ask_tool()
    assert tool["action"] == "ask_user_choice"
    schema = tool["inputSchema"]
    assert set(schema["required"]) == {"question", "options"}

    question = schema["properties"]["question"]
    assert question["minLength"] == 1
    assert question["maxLength"] == 500

    options = schema["properties"]["options"]
    assert options["minItems"] == 2
    assert options["maxItems"] == 4
    item = options["items"]
    assert set(item["required"]) == {"key", "label", "description"}
    assert item["properties"]["key"]["pattern"] == "^[a-z][a-z0-9_]{0,31}$"
    assert item["properties"]["label"]["maxLength"] == 80
    assert item["properties"]["description"]["maxLength"] == 200

    kind = schema["properties"]["kind"]
    assert kind["enum"] == ["informational", "execution_mode"]
    # kind is optional — the backend defaults it to informational.
    assert "kind" not in schema["required"]


def test_ask_user_choice_description_pins_the_posture() -> None:
    """The description IS the prompt (communicator/CLAUDE.md review bar):
    it must state the when-to-use bar, the end-turn contract (D2), the
    no-polling rule, and the one-open-question supersession rule (D6)."""
    desc = " ".join(_ask_tool()["description"].split())
    assert "ONLY when a genuine decision needs the user" in desc
    assert "ENDS your turn" in desc
    assert "the answer arrives as the user's next message" in desc
    assert "never poll" in desc
    assert "At most one open question per conversation" in desc
    assert "a new ask supersedes the old" in desc
    assert "Not available in General Chat" in desc


# ---------------------------------------------------------------------------
# Catalog placement — Manager-only, stripped in General Chat
# ---------------------------------------------------------------------------


def test_ask_user_choice_is_manager_only() -> None:
    assert "ask_user_choice" not in {t["name"] for t in get_worker_tools()}
    assert "ask_user_choice" not in {t["name"] for t in get_planner_tools()}


def test_ask_user_choice_stripped_in_general_chat() -> None:
    """No selector may fire in General Chat (implementation-plan landmine):
    the action is in the _BOARD_WRITE_ACTIONS strip set, so the
    registration-time filter removes it from a general_chat Manager
    session (the runtime guard is defense-in-depth). Exercises the REAL
    filter function main() applies (``filter_general_chat_tools``), not a
    mirror of its expression (C-6)."""
    assert "ask_user_choice" in mts._BOARD_WRITE_ACTIONS
    surviving = {
        t.get("action")
        for t in mts.filter_general_chat_tools(get_manager_tools())
    }
    assert "ask_user_choice" not in surviving


# ---------------------------------------------------------------------------
# D2 — session PRE-LOCK after a successful ask (mcp_tool_filter pattern)
# ---------------------------------------------------------------------------

_ASK_ARGS = {
    "question": "How do you want me to run it?",
    "options": [
        {
            "key": "big_assignment",
            "label": "One big assignment",
            "description": "Fastest; one expert builds it end-to-end.",
        },
        {
            "key": "program",
            "label": "A program",
            "description": "Spec, milestones, checkpoints you approve.",
        },
    ],
}


def _run_ask_session(
    *,
    backend_result: dict | None = None,
    task_mode: str = "manager",
    backend_raises: Exception | None = None,
) -> tuple[object, dict, dict]:
    """Run ask_user_choice then a follow-up tool through the REAL
    ``MCPServer._execute_tool`` with the backend call stubbed."""
    originals = {
        "TASK_MODE": mts.TASK_MODE,
        "_call_backend": mts._call_backend,
    }
    mts.TASK_MODE = task_mode

    async def _stub_backend(action: str, params: dict) -> dict:
        if backend_raises is not None:
            raise backend_raises
        return dict(backend_result or {})

    mts._call_backend = _stub_backend  # type: ignore[assignment]
    tools = [
        {"name": "ask_user_choice", "action": "ask_user_choice"},
        {"name": "get_board", "action": "get_board"},
    ]
    server = mts.MCPServer(tools)
    try:
        first = asyncio.run(
            server._execute_tool("ask_user_choice", dict(_ASK_ARGS))
        )
        second = asyncio.run(server._execute_tool("get_board", {}))
    finally:
        mts.TASK_MODE = originals["TASK_MODE"]
        mts._call_backend = originals["_call_backend"]  # type: ignore[assignment]
    return server, first, second


def test_successful_ask_locks_the_manager_session() -> None:
    server, first, second = _run_ask_session(
        backend_result={"choice_id": "abc-123", "status": "asked"},
    )
    # The ask itself succeeds and instructs the end-turn.
    assert not first.get("isError")
    first_text = first["content"][0]["text"]
    assert '"status": "asked"' in first_text
    assert "abc-123" in first_text
    assert "End your turn now" in first_text
    # The session is PRE-LOCKed exactly like the terminal board actions.
    assert server._session_locked is True
    assert second.get("isError") is True
    second_text = second["content"][0]["text"]
    assert "SESSION TERMINATED" in second_text
    assert "asked the user" in second_text
    assert "STOP" in second_text


def test_failed_ask_releases_the_lock_for_retry() -> None:
    server, first, second = _run_ask_session(
        backend_result={"error": True, "message": "duplicate option key"},
    )
    assert first.get("isError") is True
    # Terminal-action failure unlocks (same posture as move_task/update_status)
    # so the Manager can correct the options and re-ask in the same turn.
    assert server._session_locked is False
    assert not (
        second.get("isError")
        and "SESSION TERMINATED" in second["content"][0]["text"]
    )


def test_ask_raising_backend_releases_the_lock() -> None:
    """L-4 (C-3): a terminal(-locking) call whose execution RAISES — not a
    clean backend error DICT — must release the PRE-LOCK too. Before the
    fix only the error-dict path unlocked, so a raising ask wedged the
    session: "Tool error" then "SESSION TERMINATED: you asked the user"
    on every retry, with no question bubble ever posted."""
    server, first, second = _run_ask_session(
        backend_raises=RuntimeError("transport blew up"),
    )
    assert first.get("isError") is True
    assert "Tool error" in first["content"][0]["text"]
    # The lock set by the failed ask is released — the session can retry.
    assert server._session_locked is False
    assert "SESSION TERMINATED" not in second["content"][0]["text"]


def test_ask_lock_applies_to_manager_sessions_only() -> None:
    # Defense pin: the PRE-LOCK trigger is TASK_MODE == "manager". The tool
    # exists in no worker/Planner catalog, but a leaked call must not wedge
    # a worker session with a Manager-shaped lock.
    server, first, _second = _run_ask_session(
        backend_result={"choice_id": "x", "status": "asked"},
        task_mode="execute",
    )
    assert server._session_locked is False
    assert not first.get("isError")


# ---------------------------------------------------------------------------
# D3 — supersession context line (typing always wins)
# ---------------------------------------------------------------------------

_SUPERSEDED_LINE = (
    "(Your earlier question was superseded by the user's own message "
    "— honor the text, do not re-ask.)"
)


def test_supersession_line_renders_only_when_flagged() -> None:
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_context import build_dynamic_context

    store = ConfigStore()
    base = {
        "workstream_id": "11111111-1111-1111-1111-111111111111",
        "workstream_name": "Pivot2 WS",
        "workstream_priority": "high",
    }
    ctx_key = "workstream:11111111-1111-1111-1111-111111111111"

    flagged = build_dynamic_context(
        ctx_key, {**base, "choice_superseded": True}, store
    )
    assert _SUPERSEDED_LINE in flagged

    unflagged = build_dynamic_context(ctx_key, dict(base), store)
    assert "superseded by the user's own message" not in unflagged


def test_handoff_note_renders_only_when_present() -> None:
    """F3 (cross-stack review): after an own_workstream consent the origin
    got no Manager turn — until one lands, the dynamic context must tell a
    resumed session its question WAS answered and the request moved (never
    re-ask, never misread the next message as the answer)."""
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_context import build_dynamic_context

    store = ConfigStore()
    base = {
        "workstream_id": "11111111-1111-1111-1111-111111111111",
        "workstream_name": "Pivot2 WS",
        "workstream_priority": "high",
    }
    ctx_key = "workstream:11111111-1111-1111-1111-111111111111"

    noted = build_dynamic_context(
        ctx_key, {**base, "choice_handoff_note": "Wiki System"}, store
    )
    assert (
        'that request moved to the workstream "Wiki System"' in noted
    )
    assert "Do not re-ask" in noted

    plain = build_dynamic_context(ctx_key, dict(base), store)
    assert "request moved to the workstream" not in plain


# ---------------------------------------------------------------------------
# Transform — context_key injection
# ---------------------------------------------------------------------------


def test_transform_injects_manager_context_key(monkeypatch) -> None:
    monkeypatch.setenv(
        "CONTEXT_KEY", "workstream:22222222-2222-2222-2222-222222222222"
    )
    out = transform_params("ask_user_choice", None, dict(_ASK_ARGS))
    assert (
        out["context_key"]
        == "workstream:22222222-2222-2222-2222-222222222222"
    )
    # Question/options pass through untouched.
    assert out["question"] == _ASK_ARGS["question"]
    assert out["options"] == _ASK_ARGS["options"]


def test_transform_leaves_context_key_absent_without_env(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_KEY", "")
    out = transform_params("ask_user_choice", None, dict(_ASK_ARGS))
    assert "context_key" not in out


def test_transform_env_context_key_beats_model_supplied(monkeypatch) -> None:
    """L-6 (C-4): the session env CONTEXT_KEY is the turn's LOCKED context —
    it overrides UNCONDITIONALLY; a model-supplied value (hallucinated or
    stale) must never pin the choice row to another conversation."""
    monkeypatch.setenv(
        "CONTEXT_KEY", "workstream:22222222-2222-2222-2222-222222222222"
    )
    params = dict(_ASK_ARGS)
    params["context_key"] = "workstream:99999999-9999-9999-9999-999999999999"
    out = transform_params("ask_user_choice", None, params)
    assert (
        out["context_key"]
        == "workstream:22222222-2222-2222-2222-222222222222"
    )


def test_transform_drops_model_context_key_and_unknown_params(
    monkeypatch,
) -> None:
    """Without an env context the model-supplied key is DROPPED (never
    forwarded), and unknown params are stripped: the transform whitelist
    stands in for ``additionalProperties: false`` (the catalog convention
    omits it, so the CLI does not reject extras)."""
    monkeypatch.setenv("CONTEXT_KEY", "")
    params = dict(_ASK_ARGS)
    params["context_key"] = "workstream:99999999-9999-9999-9999-999999999999"
    params["surprise"] = "x"
    params["kind"] = "execution_mode"
    params["proposed_workstream_name"] = "Chess App"
    out = transform_params("ask_user_choice", None, params)
    assert "context_key" not in out
    assert "surprise" not in out
    # The whitelisted optionals still flow through.
    assert out["kind"] == "execution_mode"
    assert out["proposed_workstream_name"] == "Chess App"


# ---------------------------------------------------------------------------
# P2-3 — the Manager playbook boundary flow
# ---------------------------------------------------------------------------


def test_playbook_has_the_program_boundary_ask_block() -> None:
    """The authoritative consent block must exist, name the selector call,
    carry the EXACT option copy (concept-overview §3), and end the turn."""
    assert (
        "## The program boundary — consent in chat, never configuration"
        in _MANAGER_NORM
    )
    assert 'Ask with `ask_user_choice(kind="execution_mode")`' in _MANAGER_NORM
    assert (
        "then END your turn (asking ends it). ALWAYS these two options:"
        in _MANAGER_NORM
    )
    # Exact option copy — key, label, tradeoff description.
    assert (
        'key `big_assignment`, label "One big assignment", description '
        '"Fastest; one expert builds it end-to-end; you review the result."'
        in _MANAGER_NORM
    )
    assert (
        'key `program`, label "A program", description "Spec, milestones, '
        'checkpoints where you approve before we continue."' in _MANAGER_NORM
    )
    # Reply-turn routing.
    assert "route as Tier 1b (one fat task to one expert)" in _MANAGER_NORM
    assert "the program machinery is ALREADY unlocked" in _MANAGER_NORM


def test_playbook_pins_the_anti_nag_hard_rules() -> None:
    """D6 (repinned for C-1/C-5): silent classification below the boundary;
    the never-ask rule is scoped to execution_mode asks (informational asks
    have a legitimate lane); explicit program wording is a true skip ONLY
    in an already-consented program — otherwise it means run the selector
    immediately as a one-click confirmation (typed consent has NO backend
    application path); never re-ask; one open question."""
    assert "Classification is SILENT." in _MANAGER_NORM
    assert (
        "NEVER ask an execution_mode question for asks, assignments, "
        "scripts, or ops" in _MANAGER_NORM
    )
    # C-5: informational asks are scoped in, not banned.
    assert (
        "An informational ask is for a genuine either-or only the USER can "
        "pick — never one you can decide." in _MANAGER_NORM
    )
    # C-1: the explicit-wording branch no longer over-promises "proceed".
    assert '"Set this up as a project with milestones"' in _MANAGER_NORM
    assert (
        "skips the selector ONLY where this workstream ALREADY runs a "
        "consented program" in _MANAGER_NORM
    )
    assert (
        "typing cannot apply consent (only the click does)" in _MANAGER_NORM
    )
    assert (
        "run the selector IMMEDIATELY as a one-click confirmation"
        in _MANAGER_NORM
    )
    assert (
        "(\"You said program — confirm and I'll set it up\")" in _MANAGER_NORM
    )
    assert "never announce you are proceeding first" in _MANAGER_NORM
    # NEGATIVE: the retired proceed-then-get-refused copy is gone.
    assert "proceed as a program directly" not in _MANAGER_NORM
    assert (
        "if the backend still refuses, ask the selector once"
        not in _MANAGER_NORM
    )
    assert '"quick and dirty" → big_assignment, no selector' in _MANAGER_NORM
    assert "NEVER re-ask for the same assignment" in _MANAGER_NORM
    assert "at most ONE open question per conversation" in _MANAGER_NORM
    assert "honor the text" in _MANAGER_NORM


def test_playbook_pins_the_never_flip_yourself_rule() -> None:
    """D1: the Manager can never assert consent — the backend applies the
    side effect from the user's own click."""
    assert (
        "You cannot change a workstream's execution mode yourself and never "
        "attempt it" in _MANAGER_NORM
    )
    assert (
        "consent is applied backend-side from the user's own click, never "
        "from anything you do" in _MANAGER_NORM
    )


def test_playbook_treats_teaching_errors_as_the_cue_to_ask() -> None:
    assert "A consent-gate refusal is your cue to ask." in _MANAGER_NORM
    assert (
        "that teaching error is the SIGNAL to run the selector (once) — "
        "never an error message to show the user" in _MANAGER_NORM
    )


def test_playbook_carries_the_workstream_mental_model_line() -> None:
    """P3-1 retarget: separate vectors are OFFERED via option C and created
    by the backend only from the user's click — never recommended as a
    manual user chore, never created by the Manager (D5)."""
    assert "One workstream = one project/program." in _MANAGER_NORM
    assert (
        "Separate vectors get their own workstream — offered via option C, "
        "created only from the user's click" in _MANAGER_NORM
    )
    assert "you never create workstreams yourself" in _MANAGER_NORM


def test_playbook_never_instructs_the_settings_dial_flip() -> None:
    """NEGATIVE pin: the retired dial copy must not resurface — the Manager
    never names the work_mode column and never sends the user to a settings
    surface to enable programs (the P2-4 UI dial is removed)."""
    assert "work_mode" not in _MANAGER_NORM
    assert "switch the workstream to program mode" not in _MANAGER_NORM
    assert "switch this workstream to program mode" not in _MANAGER_NORM
    assert "Workstream panel" not in _MANAGER_NORM
    assert "in default work mode the backend refuses" not in _MANAGER_NORM


def test_default_mode_banner_points_at_the_selector() -> None:
    """The dynamic-context default-mode banner must route the Manager to the
    selector, never to a settings flip (the pivot-1 banner pin keeps the
    assignments-only strings; this pins the retargeted tail)."""
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_context import build_dynamic_context

    ctx = build_dynamic_context(
        "workstream:11111111-1111-1111-1111-111111111111",
        {
            "workstream_id": "11111111-1111-1111-1111-111111111111",
            "workstream_name": "Pivot2 WS",
            "workstream_priority": "high",
            "work_mode": "default",
        },
        ConfigStore(),
    )
    assert 'ask via `ask_user_choice(kind="execution_mode")`' in ctx
    assert "never send them to settings" in ctx
    assert "switch this workstream to program mode" not in ctx


def test_execution_mode_kind_description_names_the_consent_flow() -> None:
    """The kind description must describe the live Phase-2 consent flow —
    the Phase-1 'RESERVED … do not use it yet' placeholder is retired."""
    desc = " ".join(
        _ask_tool()["inputSchema"]["properties"]["kind"]["description"].split()
    )
    assert "do not use it yet" not in desc
    assert "RESERVED" not in desc
    assert "the backend applies the user's click itself" in desc
    assert "you never set or change the mode yourself" in desc


# ---------------------------------------------------------------------------
# P3-1 — option C: a program in its own workstream
# ---------------------------------------------------------------------------


def test_proposed_workstream_name_schema() -> None:
    """The fixed contract with the backend sibling: an optional string param,
    1-100 chars, NOT in required (it is conditionally required — only when an
    own_workstream option is included; the backend enforces that)."""
    schema = _ask_tool()["inputSchema"]
    prop = schema["properties"]["proposed_workstream_name"]
    assert prop["type"] == "string"
    assert prop["minLength"] == 1
    assert prop["maxLength"] == 100
    assert "proposed_workstream_name" not in schema["required"]


def test_proposed_workstream_name_description_pins_the_contract() -> None:
    """The param description must state when it is required and that the
    BACKEND creates the workstream from the user's click (D5)."""
    desc = " ".join(
        _ask_tool()["inputSchema"]["properties"][
            "proposed_workstream_name"
        ]["description"].split()
    )
    assert (
        "REQUIRED whenever an option with key 'own_workstream' is included"
        in desc
    )
    assert "short, human, 2-4 words — the project's name, not a sentence" in (
        desc
    )
    assert "the BACKEND creates the workstream from this name" in desc
    assert "you never create workstreams yourself" in desc


def test_ask_description_pins_own_workstream_semantics() -> None:
    """The tool description's option guidance: own_workstream is valid on
    execution_mode questions only, requires the proposed name, and the
    backend — never the Manager — does the creation."""
    desc = " ".join(_ask_tool()["description"].split())
    assert "Option key 'own_workstream'" in desc
    assert "valid on execution_mode questions ONLY" in desc
    assert "include it only together with proposed_workstream_name" in desc
    assert (
        "the backend creates that workstream from the user's click and "
        "moves the request there, never you" in desc
    )


def test_playbook_pins_option_c_inclusion_conditions() -> None:
    """WHEN to include option C: (a) this workstream already runs a live
    program, or (b) the request is clearly a separate vector/project."""
    assert (
        "ONLY when (a) this workstream already runs a live program (a spec "
        "with milestones exists, or a live scope), or (b) the request is "
        "clearly a separate vector/project from this workstream's purpose."
        in _MANAGER_NORM
    )


def test_playbook_pins_option_c_copy_template() -> None:
    """The exact option copy template + the proposed-name guidance.
    NOTE: the template source doubles literal braces (PC-L1 .format), so
    the raw-template pin matches {{proposed name}}."""
    assert (
        'key `own_workstream`, label "A program in its own workstream", '
        'description "Same as a program, in a dedicated space: '
        '{{proposed name}}."' in _MANAGER_NORM
    )
    assert (
        "pass `proposed_workstream_name` (short, human, 2-4 words — the "
        "project's name, not a sentence)" in _MANAGER_NORM
    )


def test_playbook_pins_the_post_click_do_nothing_rule() -> None:
    """After the click the backend sets everything up and moves the request
    there. Repinned (C-6): the old copy scripted a reply turn the Manager
    never gets (the backend suppresses the origin turn for own_workstream
    clicks) — the playbook now states that reality."""
    assert (
        "the backend sets everything up from the click — creates the "
        "workstream, posts the hand-off chip here, and moves the request "
        "into the new workstream's context" in _MANAGER_NORM
    )
    assert (
        "You will NOT get a turn in this chat for that click — the backend "
        "handles everything" in _MANAGER_NORM
    )
    # NEGATIVE: the retired reply-turn-scripting copy is gone.
    assert "Post NOTHING further in this chat" not in _MANAGER_NORM
    assert "the hand-off chip is the answer here" in _MANAGER_NORM
    assert "the program continues in the new workstream" in _MANAGER_NORM


def test_playbook_states_the_reply_arrives_as_a_plain_user_row() -> None:
    """C-6 (rotated-session robustness): the reply-turn block states the
    click arrives as a plain user message row — "Selected: {label}" — so a
    Manager on a fresh/rotated session still recognizes the answer.
    (Raw-template pin: PC-L1 .format doubles the literal braces.)"""
    assert (
        "On the reply turn (the click arrives as a plain user row, "
        '"Selected: {{label}}" — even in a fresh/rotated session):'
        in _MANAGER_NORM
    )


def test_playbook_never_instructs_manager_workstream_creation() -> None:
    """NEGATIVE (D5 unchanged): no instruction for the Manager to create
    workstreams itself — there is no such tool and no such copy; creation
    happens only on the backend reply path from the user's click. The
    retired 'recommend a new workstream to the user' manual-chore copy is
    gone too."""
    assert "create_workstream" not in _MANAGER_NORM
    assert "create the workstream yourself" not in _MANAGER_NORM
    assert "recommend a new workstream to the user" not in _MANAGER_NORM
    assert "you never create workstreams yourself" in _MANAGER_NORM


def test_workstream_template_offers_option_c_not_a_user_chore() -> None:
    """The per-workstream CLAUDE.md's new-project bullet must match the P3-1
    posture: offered via the selector (option C), backend-created from the
    click — not 'the user creates it' manual copy."""
    from src.config_sync.claude_md_templates._workstream import (
        generate_workstream_claude_md,
    )

    text = " ".join(
        generate_workstream_claude_md(
            {"short_code": "WS", "name": "Pin WS"}
        ).split()
    )
    assert (
        "the Manager offers a NEW workstream via the chat selector "
        "(option C)" in text
    )
    assert (
        "the backend creates it from the user's click; the Manager never "
        "creates it" in text
    )
    assert "the user creates it — the Manager never does" not in text
