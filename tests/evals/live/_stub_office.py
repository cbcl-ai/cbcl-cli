"""Deterministic synthetic office that answers read-only tool calls.

The API lane's decision loop lets the model look around (board, roster, files,
spec) exactly as it would in production, but every read is answered from this
fixed data so results are reproducible and no real office is touched. The
first NON-read tool call is the recorded decision and is never executed.

Pins keep this stub honest (``tests/evals/test_live_eval_no_leakage.py``):

* ``READ_ONLY_MANAGER_TOOLS`` must be a subset of the production Manager
  catalog, so a renamed tool cannot silently fall out of the read set, and no
  write tool can ever be answered as if it were a read.
* ``StubOffice.team_roster()`` must equal the backend's rendered roster
  (``app.ws.context_builder._build_team_roster``: preamble, section headings,
  order and entries), so the Manager sees the roster it sees in production.
* ``_CONSULT_PATHS`` must equal the backend's consult-only set
  (``app.tasks.task_service._CONSULT_ONLY_AGENTS``), so consult-only agents
  are annotated and never counted as assignable.

The stub itself imports nothing from the backend: standalone CLI checkouts
have no backend, and the pins skip there.
"""

from __future__ import annotations

import copy
import dataclasses
import uuid
from typing import Any

# Manager tools whose effect is a read (a stub answer is safe and honest).
# ``get_action_request`` records a scoped read receipt in production; the stub
# answers it as a plain lookup (no receipt, no decision).
READ_ONLY_MANAGER_TOOLS = frozenset({
    "inspect_configuration",
    "get_board",
    "get_task_detail",
    "get_flow_run",
    "list_scopes",
    "get_scope",
    "get_action_request",
    "list_assignment_schedules",
    "list_scripts",
    "list_office_secrets",
    "list_office_secret_usage",
    "get_script",
    "list_script_executions",
    "list_script_templates",
    "get_script_template",
    "list_agents",
    "search_kb",
    "get_kb_document",
    "list_files",
    "get_file",
    "get_collection",
    "query_rows",
    "recall",
    "get_chat_history",
    "get_execution_plan",
    "get_spec",
})

WORKSTREAM_ID = "11111111-1111-1111-1111-111111111111"
_PROFILE_NAMESPACE = uuid.UUID("7d1f2c3a-5b6e-4f70-8a91-b2c3d4e5f607")

# The backend roster preamble (``_build_team_roster``) — kept verbatim so the
# Manager reads the same framing it reads in production.
ROSTER_PREAMBLE = (
    "Profiles describe reusable expertise and access. Assign tasks by the profile "
    "slug below; the platform allocates a separate task-owned agent UUID. Retained "
    "agents are not necessarily running processes. Ready counts describe demand, "
    "not free capacity. Real dependencies and office/runtime limits still apply; "
    "do not create duplicate profiles to gain concurrency."
)
# The backend consult-only set (``task_service._CONSULT_ONLY_AGENTS``),
# kept verbatim: name → (display label, "engaged through …" phrase).
_CONSULT_PATHS = {
    "planner": (
        "The Planner",
        "the `consult_planner` tool (async)",
    ),
    "flow-architect": (
        "The Flow Architect",
        "an async flow-design consult (the Studio's design rail — "
        "`POST /api/offices/{office_id}/flows/{flow_id}/design`)",
    ),
    "data-curator": (
        "The Data Curator",
        "an async collections-curate consult (the Data page's command "
        "box — `POST /api/offices/{office_id}/collections/curate`)",
    ),
}


@dataclasses.dataclass(frozen=True)
class RosterAgent:
    name: str
    display_name: str
    role_description: str
    agent_type: str = "custom"
    model: str = "opus"
    avatar_emoji: str = "🤖"
    allowed_tools: tuple[str, ...] = ("Read", "Write", "Bash", "Glob", "Grep")
    skills: tuple[str, ...] = ()

    @property
    def profile_id(self) -> str:
        return str(uuid.uuid5(_PROFILE_NAMESPACE, self.name))

    def roster_entry(self) -> list[str]:
        """One roster entry in the backend ``_format_agent_entry`` shape."""
        lines = [
            f"**{self.display_name}** ({self.name}) — {self.avatar_emoji}",
            f"- Profile ID: {self.profile_id} (configuration, not a task agent UUID)",
            f"- Role: {self.role_description}",
            f"- Model: {self.model}",
            f"- Tools: {', '.join(self.allowed_tools)}",
        ]
        consult = _CONSULT_PATHS.get(self.name)
        if consult:
            annotation = (
                f"- ⚠️ HOW TO USE: consult-only — engaged ONLY through {consult[1]}. "
                "NEVER `create_task` assigned to it and never set it as a `reviewer` "
                "(the backend rejects both)."
            )
            if self.name == "planner":
                annotation += " See 'Working with the Planner' in CLAUDE.md."
            lines.append(annotation)
        if self.skills:
            lines.append(f"- Skills: {', '.join(self.skills)}")
        return lines

    def as_tool_row(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "agent_type": self.agent_type,
            "role_description": self.role_description,
            "model": self.model,
            "allowed_tools": list(self.allowed_tools),
            "is_active": True,
            "skills": list(self.skills),
            "connectors": [],
        }


SYSTEM_ROSTER = (
    RosterAgent(
        "analyst", "Analyst",
        "Research standards — produces the office's read-deliverables (research, "
        "comparisons, decision briefs) to a citable, triangulated standard.",
        agent_type="system", avatar_emoji="🔍",
    ),
    RosterAgent(
        "auditor", "Auditor",
        "Quality control — independent verification of deliverables against "
        "acceptance criteria. Verifies, never fixes.",
        agent_type="system", avatar_emoji="📋",
    ),
    RosterAgent(
        "builder", "Builder",
        "Execution — the accountable executor for cohesive builds: prototypes, "
        "apps, documents, sites.",
        agent_type="system", avatar_emoji="🔨",
    ),
    RosterAgent(
        "manager-assistant", "Manager Assistant",
        "Chief of staff — the fast, economical tier: quick lookups and checks, "
        "smoke reviews, board triage.",
        agent_type="system", model="sonnet", avatar_emoji="⚡",
    ),
    RosterAgent(
        "planner", "Planner",
        "Contracts — drafts the specs you sign and independently judges milestone "
        "gates. Consult-only.",
        agent_type="system", avatar_emoji="🗺️",
    ),
)
CONSULT_ONLY = frozenset(_CONSULT_PATHS)


@dataclasses.dataclass
class StubOffice:
    """Fixed office state for one case. Mutable only through ``dispatch`` logs."""

    office_name: str
    workstream_name: str
    roster: tuple[RosterAgent, ...]
    workstream_id: str = WORKSTREAM_ID
    work_mode: str = "default"
    workstream_description: str = ""
    workstream_goals: str = ""
    files: tuple[dict, ...] = ()
    tasks: tuple[dict, ...] = ()
    spec: dict | None = None
    read_log: list[tuple[str, dict]] = dataclasses.field(default_factory=list)

    @property
    def context_key(self) -> str:
        return f"workstream:{self.workstream_id}"

    def team_roster(self) -> str:
        """The roster text in the backend ``_build_team_roster`` layout."""
        lines = [ROSTER_PREAMBLE, ""]
        sections = (
            ("### System Agent Profiles (always active)", "system"),
            ("### Custom Agent Profiles", "custom"),
        )
        for heading, agent_type in sections:
            members = [agent for agent in self.roster if agent.agent_type == agent_type]
            if not members:
                continue
            lines += [heading, ""]
            for agent in members:
                lines += agent.roster_entry() + [""]
        return "\n".join(lines).strip()

    def context_data(self, **overrides: Any) -> dict:
        """The per-turn context the backend would ship for this office."""
        data = {
            "office_name": self.office_name,
            "workstream_id": self.workstream_id,
            "workstream_name": self.workstream_name,
            "workstream_priority": "medium",
            "workstream_description": self.workstream_description,
            "workstream_goals": self.workstream_goals,
            "work_mode": self.work_mode,
            "team_roster": self.team_roster(),
            "board_summary": {},
            "scopes": [],
        }
        data.update(overrides)
        return data

    def assignable_names(self) -> set[str]:
        """Roster profiles that may be assignees or reviewers."""
        return {agent.name for agent in self.roster} - CONSULT_ONLY

    def dispatch(self, tool_name: str, params: dict) -> dict:
        """Answer one read-only tool call deterministically."""
        if tool_name not in READ_ONLY_MANAGER_TOOLS:
            raise ValueError(f"{tool_name} is not a read-only tool; it is a decision")
        self.read_log.append((tool_name, copy.deepcopy(params)))
        handler = getattr(self, f"_read_{tool_name}", None)
        if handler is not None:
            return handler(params)
        return self._not_available(tool_name)

    # ── Answers ──────────────────────────────────────────────────────

    @staticmethod
    def _not_available(tool_name: str) -> dict:
        return {"items": [], "count": 0, "note": f"No {tool_name} data in this office."}

    def _read_list_agents(self, params: dict) -> dict:
        return {"agents": [agent.as_tool_row() for agent in self.roster]}

    def _read_get_board(self, params: dict) -> dict:
        return {"tasks": list(copy.deepcopy(self.tasks)), "total": len(self.tasks)}

    def _read_list_scopes(self, params: dict) -> dict:
        return {"scopes": [], "count": 0}

    def _read_get_scope(self, params: dict) -> dict:
        return {"error": True, "message": "Scope not found."}

    def _read_get_task_detail(self, params: dict) -> dict:
        wanted = params.get("task_id")
        for task in self.tasks:
            if wanted in (task.get("id"), task.get("readable_id")):
                return copy.deepcopy(task)
        return {"error": True, "message": "Task not found."}

    def _read_get_spec(self, params: dict) -> dict:
        if self.spec is None:
            return {"spec": None, "note": "This workstream has no spec."}
        return {"spec": copy.deepcopy(self.spec)}

    def _read_get_execution_plan(self, params: dict) -> dict:
        return {"plan": None}

    def _read_list_files(self, params: dict) -> dict:
        return {"files": list(copy.deepcopy(self.files)), "total": len(self.files)}

    def _read_get_file(self, params: dict) -> dict:
        wanted = params.get("file_id")
        for item in self.files:
            if wanted in (item.get("id"), item.get("file_path")):
                return copy.deepcopy(item)
        return {"error": True, "message": "File not found."}

    def _read_search_kb(self, params: dict) -> dict:
        return {"results": [], "total": 0}

    def _read_get_kb_document(self, params: dict) -> dict:
        return {"error": True, "message": "Document not found."}

    def _read_recall(self, params: dict) -> dict:
        return {"memories": [], "more": 0}

    def _read_get_chat_history(self, params: dict) -> dict:
        return {"messages": [], "has_more": False}

    def _read_inspect_configuration(self, params: dict) -> dict:
        return {
            "office": {"name": self.office_name, "instructions": ""},
            "workstreams": [{"id": self.workstream_id, "name": self.workstream_name,
                             "instructions": ""}],
            "agents": [agent.as_tool_row() for agent in self.roster],
        }

    def _read_get_action_request(self, params: dict) -> dict:
        return {"error": True, "message": "Action request not found."}

    def _read_get_flow_run(self, params: dict) -> dict:
        return {"error": True, "message": "Flow run not found."}

    def _read_get_collection(self, params: dict) -> dict:
        return {"error": True, "message": "Collection not found."}


def uploaded_file(file_id: str, path: str, title: str, size: int) -> dict:
    """An Office Files row as ``list_files`` returns it."""
    return {
        "id": file_id,
        "title": title,
        "file_path": path,
        "size_bytes": size,
        "attached_task_ids": [],
    }
