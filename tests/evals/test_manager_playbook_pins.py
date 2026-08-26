"""Manager-playbook surface pins (2026-08-26 review round).

Three fact families in the Manager playbook drifted from the platform and
were corrected in this round; each pin locks the corrected sentence so the
next edit cannot silently reintroduce the stale claim (T5.3.7: every factual
prompt change updates-or-adds the eval that pins the fact).

1. **No double-announce on Planner consults.** The backend posts a visible
   "🗺️ Planner engaged — …" chat bubble on EVERY Manager-initiated
   `consult_planner` (backend/app/ws/tool_endpoint/_handlers_planner.py,
   `emit_system_chat_message`) and a finish bubble with a "→ Next:" line
   (`_emit_planner_completed`). The playbook's old "tell the user in the
   same turn that you've engaged the Planner" mandate predated those bubbles
   (rule 22b6ee2a, 2026-06-02; bubbles 245492a9, ~13h later) and produced a
   second redundant "Planner engaged" message on every consult. The rewrite
   keeps the load-bearing half — the `[Planner] …` poke is NOT user-visible,
   so its RESULT must still be summarized.

2. **Session-lock trigger list is exhaustive.** The MCP server PRE-LOCKs the
   Manager session after a successful `ask_user_choice`
   (mcp_tool_server.py, pivot-2 P1 D2 — the same mechanism as the terminal
   `move_task` verdicts), so the "Per-Turn Session Lock" section's
   "After you call any of:" list must name it.

3. **The Scripts intro names real surfaces.** The worker tool is
   `execute_script` (never the phantom `script.execute(...)`), the /scripts
   area is user-facing "Operations" (pivot-1 P2-6), and the trigger
   enumeration includes the webhook (2026-07-16) and flow-run script-step
   (Flow Studio) paths.
"""
from __future__ import annotations

import inspect
import re

from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD


def _norm() -> str:
    """Whitespace-collapsed template (the prose is line-wrapped)."""
    return re.sub(r"\s+", " ", MANAGER_CLAUDE_MD)


# ---------------------------------------------------------------------------
# 1 — Planner consult visibility: platform announces, the Manager must not
# ---------------------------------------------------------------------------


def test_playbook_states_platform_posts_planner_bubbles() -> None:
    norm = _norm()
    assert 'The platform posts "Planner engaged" and finish bubbles' in norm


def test_playbook_forbids_reannouncing_the_engagement() -> None:
    norm = _norm()
    assert "do NOT re-announce the engagement" in norm


def test_playbook_keeps_the_poke_summarize_half() -> None:
    # The `[Planner] …` poke content is NOT user-visible — summarizing the
    # RESULT stays mandatory even though the engagement announce is gone.
    norm = _norm()
    assert "is NOT user-visible: SUMMARIZE the result before you act on it" in norm


def test_playbook_dropped_the_double_announce_mandate() -> None:
    # Negative pins: the pre-bubble-era mandates must not resurface.
    norm = _norm()
    assert "tell the user in the same turn that you've engaged the Planner" not in norm
    assert "I've engaged the Planner to spec this out" not in norm


# ---------------------------------------------------------------------------
# 2 — Per-Turn Session Lock: ask_user_choice is a named trigger
# ---------------------------------------------------------------------------


def test_session_lock_section_names_ask_user_choice_trigger() -> None:
    section = MANAGER_CLAUDE_MD.split("## Per-Turn Session Lock", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "`move_task`" in section  # the original trigger stays
    assert "`ask_user_choice`" in section
    assert "asking ends the turn" in section


def test_ask_user_choice_prelock_exists_in_server() -> None:
    # The premise: the MCP server really PRE-LOCKs after ask_user_choice in
    # manager mode (pivot-2 P1 D2). If this branch is ever removed, the
    # playbook bullet above becomes the stale claim and must go too.
    from src._agent_image import mcp_tool_server as mts

    src = inspect.getsource(mts)
    assert 'action == "ask_user_choice" and TASK_MODE == "manager"' in src


# ---------------------------------------------------------------------------
# 3 — Scripts intro: real tool name, real page label, honest trigger list
# ---------------------------------------------------------------------------


def _scripts_section() -> str:
    section = MANAGER_CLAUDE_MD.split("## Scripts, Schedules, and Callbacks", 1)[1]
    return section.split("\n## ", 1)[0]


def test_scripts_intro_names_execute_script_not_the_phantom() -> None:
    section = _scripts_section()
    assert "`execute_script`" in section
    assert "script.execute" not in MANAGER_CLAUDE_MD  # the phantom tool


def test_scripts_intro_says_operations_page() -> None:
    # pivot-1 P2-6: the /scripts area is user-facing "Operations"
    # (frontend_v2.1 RouteAnnouncer / CommandPalette).
    section = _scripts_section()
    assert "Operations page" in section
    assert "Scripts page" not in MANAGER_CLAUDE_MD


def test_scripts_intro_enumerates_the_extra_trigger_paths() -> None:
    # Webhooks (docs/02-domain/scripts.md §4.3d) and flow-run script steps
    # (Flow Studio action/run_script blocks) are real trigger paths — the
    # intro must not claim "one of three ways".
    section = re.sub(r"\s+", " ", _scripts_section())
    assert "webhook" in section
    assert "flow-run script step" in section
    assert "one of three ways" not in _norm()
