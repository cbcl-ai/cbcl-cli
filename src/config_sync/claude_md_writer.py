"""CLAUDE.md writer — writes CLAUDE.md files for office, manager, agents, workstreams.

Responsible for:
- Writing the shared office-level CLAUDE.md (auto-discovered by ALL agents)
- Writing the Manager-specific CLAUDE.md (in agents/manager/)
- Writing per-agent CLAUDE.md files (system agents from constants, custom from config)
- Writing per-workstream CLAUDE.md files
- Cleaning up orphan directories when agents/workstreams are removed
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from src.config_sync._descriptor_io import (
    MaterializationFailures,
    atomic_replace_file,
    ensure_owned_directory,
    is_environmental_error,
    is_regular_file,
    list_real_directories,
    open_owned_subdirectory,
    open_workspace_root,
    remove_directory_tree,
)
from src.config_sync.claude_md_content import (
    SHARED_OFFICE_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
    generate_custom_agent_claude_md,
    generate_workstream_claude_md,
)
from src.config_sync.claude_md_templates._shared_agent import (
    bash_capability_rules_for,
)
from src.config_sync.office_work_policy import (
    render_office_work_policy,
    render_pinned_work_policy,
    work_policy_from_config,
)
from src.config_sync.workstream_dirs import (
    WorkstreamLayout,
    canonical_workstream_id,
    has_real_directories,
    open_workstreams_root,
    remove_workstream_claude_md,
    write_workstream_claude_md,
)
from src.paths import is_safe_agent_name

AGENTS_DIRNAME = "agents"
MANAGER_DIRNAME = "manager"
CLAUDE_MD_FILENAME = "CLAUDE.md"

# PreToolUse Bash-guard hook config written into every agent's
# ``.claude/settings.json`` (Tier 3 worker-session-churn fix). Claude
# Code auto-loads ``<project>/.claude/settings.json``; the worker's
# project dir is ``/workspace/agents/<name>/``, so this wires the guard
# at ``/opt/cubicle/bash_guard.py`` (baked into the agent image) into
# every worker session. The Bash hook denies unbounded monitors; the wildcard
# execution-pace hook adds at most two elapsed-time reminders. It is inert for
# Manager sessions, which do not receive a task-run clock.
_AGENT_HOOK_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 /opt/cubicle/bash_guard.py",
                    }
                ],
            },
            {
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": "python3 /opt/cubicle/execution_pace.py",
                    "timeout": 3,
                }],
            },
        ]
    }
}


def write_agent_hook_settings_at(claude_fd: int) -> None:
    """Replace ``settings.json`` in an OPEN ``.claude`` directory.

    ``settings.json`` is replaced by a no-follow rename (a link planted at
    it is replaced, not written through); the caller owns the directory
    descriptor and its ownership. A retained task-Agent workspace uses this
    on a ``.claude`` it created root-owned, before handing the directory to
    the agent uid. Errors propagate.
    """
    atomic_replace_file(
        claude_fd, "settings.json", json.dumps(_AGENT_HOOK_SETTINGS, indent=2)
    )


def _write_hook_settings_in(agent_fd: int, label: str) -> None:
    """Write per-agent Bash protection and bounded execution-pace guidance
    into the OPEN agent directory ``agent_fd``.

    Idempotent (overwritten on every sync) and non-fatal: an ``OSError`` is
    logged and sync continues, since a missing guard only loses
    defense-in-depth. ``.claude`` is created, opened relative to
    ``agent_fd`` without following a link and owned through ``fchown``, so
    a link a session planted at ``.claude`` or ``settings.json`` never
    redirects the write or its ownership change.
    """
    try:
        with open_owned_subdirectory(agent_fd, ".claude") as claude_fd:
            write_agent_hook_settings_at(claude_fd)
    except OSError as exc:
        logger.warning(
            "Failed to write Bash-guard settings.json for %s: %s", label, exc
        )


logger = logging.getLogger(__name__)


# T5.2.13 (06/I-5): provenance marker. Generated content carries this
# sentinel as its first line; the writer strips it before rendering. Since
# instruction-sources-v2 (2026-09-03) the sentinel is PROVENANCE DISPLAY
# only — both generated and owner-typed office instructions/agent notes are
# delivered under a follow-with-precedence wrapper (distinct headings show
# which is which). The former hard "never follow" fence for sentinel-less
# content was retired: both are authenticated, role-floored writes
# (office instructions: office ADMIN via PUT /offices/{oid}; agent notes:
# office MANAGER via PUT /agents/{id} — the SAME route that writes the
# agent's entire system_prompt unfenced), so the fence added no security
# while neutralizing the feature (production offices shipped carefully
# written operating rules the Manager was told to ignore).
GENERATED_CONTENT_SENTINEL = "<!-- cubicle:generated -->"


def _is_generated_content(content: str) -> bool:
    return content.lstrip().startswith(GENERATED_CONTENT_SENTINEL)


def _strip_generated_sentinel(content: str) -> str:
    stripped = content.lstrip()
    if stripped.startswith(GENERATED_CONTENT_SENTINEL):
        return stripped[len(GENERATED_CONTENT_SENTINEL):].lstrip("\n")
    return content


def _append_precedence_section(
    base: str, *, heading: str, note: str, body: str
) -> str:
    """The ONE shape every office-authored addition takes since the
    trust unification: the platform base, a horizontal rule, a
    provenance HEADING, a follow-with-precedence NOTE, the content. Four
    call sites (manager/agent × generated/owner-typed) differ only in
    wording — hand-spelling the block four times had already drifted the
    precedence sentence between them."""
    return f"{base}\n\n---\n\n{heading}\n\n{note}\n\n{body}\n"


# Retained task-Agent CLAUDE.md note (agent_instance_workspace). It sits
# directly below the platform playbook and ABOVE any Office Notes /
# Office-Specific Playbook section (X51), so it never reads as office text
# ranked below "the system rules above win".
RETAINED_TASK_AGENT_NOTE = (
    "\n\n## Retained task Agent configuration\n"
    "This task Agent keeps the Profile instructions, office notes and "
    "assigned skills it started with; the platform rules above are current. "
    "Assigned skills, if any, are listed under Skills and copied into "
    "`.claude/skills/` in this Agent directory — `Read` them there. The "
    "office catalog at `/workspace/.claude/skills/` can hold newer or "
    "unassigned playbooks and is not this Agent's assignment. Non-secret "
    "params.json values refresh when an attempt starts.\n"
)


# Phase 10 (T10.2.4): the static fallback shown in the office CLAUDE.md "Office
# Specs" index when no approved office-shared spec exists yet. Keeps the
# discovery instruction so agents can still find any specs that landed on disk
# out-of-band.
_OFFICE_SPECS_FALLBACK = (
    "_No office-shared specs are approved yet. If a task references one, "
    "`ls /workspace/specs/office/` to discover any that exist on disk._"
)

# Cap the rendered index so it never balloons the office header (≤15 lines —
# one line per spec). A larger spec set degrades gracefully to "+N more".
_OFFICE_SPECS_MAX_ROWS = 15


def _filter_office_specs(specs: list[dict]) -> list[dict]:
    """Office-SHARED specs only — those with no ``workstream_id``.

    Workstream specs are surfaced per-task (STEP 0.0a), never in the
    office-wide index. Mirrors ``ConfigStore.get_office_specs`` so the
    filter rule lives in one conceptual place.
    """
    return [s for s in (specs or []) if not s.get("workstream_id")]


_MARKDOWN_SPECIALS = re.compile(r"([\\`*_\[\]<>#|~])")


def _single_line(value: object) -> str:
    """Control/format characters and line breaks collapse to one space."""
    text = "".join(
        " " if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") else char
        for char in str(value or "")
    )
    return " ".join(text.split())


def _index_label(value: object) -> str:
    """One inert markdown line with markdown punctuation escaped (X49)."""
    return _MARKDOWN_SPECIALS.sub(r"\\\1", _single_line(value))


def render_office_specs_index(specs: list[dict]) -> str:
    """Render the office-shared spec index (name + one-liner + path).

    ``specs`` is the full ``config["specs"]`` list (mixed office + workstream
    specs); this filters to the office-shared ones and renders ≤15 lines.
    Returns the static ``ls`` fallback when there are no office-shared specs.
    """
    office_specs = _filter_office_specs(specs)
    if not office_specs:
        return _OFFICE_SPECS_FALLBACK

    rows: list[str] = []
    for spec in office_specs[:_OFFICE_SPECS_MAX_ROWS]:
        # X49: the name is office-member-editable data rendered into the
        # office CLAUDE.md every agent auto-loads — one line, no markdown.
        name = _index_label(spec.get("name")) or "(unnamed)"
        # Rendered in a code span: one line, no backticks.
        path = _single_line(spec.get("path")).replace("`", "")
        rev = spec.get("revision")
        rev_str = f" (rev {rev})" if rev is not None else ""
        if path:
            rows.append(f"- **{name}**{rev_str} — `{path}`")
        else:
            rows.append(f"- **{name}**{rev_str}")

    overflow = len(office_specs) - _OFFICE_SPECS_MAX_ROWS
    if overflow > 0:
        rows.append(
            f"- _…and {overflow} more under `/workspace/specs/office/`._"
        )
    return "\n".join(rows)


class ClaudeMdWriter:
    """Writes and manages CLAUDE.md files in the workspace."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)

    def sync_all(self, config: dict, *, agent_roster_summary: bool = False) -> None:
        """Full sync — write all CLAUDE.md files from config.

        ``agent_roster_summary`` marks an agent roster in the REST
        ``GET /agents`` summary shape (the daemon's startup bootstrap): its
        skills carry no description or parameter schema and its connectors
        no ``is_enabled``/connection type. Existing agent CLAUDE.md files —
        rendered from a full ``sync_config`` — are then kept instead of being
        overwritten with the poorer content (X53); the connector WebSocket's
        ``sync_config`` that follows renders the authoritative files.
        """
        # A failed write does not stop the rest: every step runs, then one
        # OSError names the failures, so config sync keeps admission closed
        # and retries (a planted link or file is skipped, never an error).
        failures = MaterializationFailures()
        steps = (
            self.ensure_directory_structure,
            lambda: self.write_office_claude_md(config),
            lambda: self.write_manager_claude_md(config),
            lambda: self.sync_agent_directories(
                config.get("agents", []),
                office_work_policy=work_policy_from_config(config),
                keep_existing=agent_roster_summary,
            ),
            lambda: self.sync_workstream_directories(config.get("workstreams", [])),
        )
        for step in steps:
            try:
                step()
            except OSError as exc:
                failures.merge(exc)
        failures.raise_if_any()

    @contextmanager
    def _open_agents_root(self, workspace_fd: int) -> Iterator[int]:
        """Create (if needed) and open ``agents`` under the open workspace
        without following a link; a link or file at that name raises
        ``OSError``. The workspace root itself is opened by the caller: its
        failure is always fatal, unlike an entry below it."""
        with open_owned_subdirectory(workspace_fd, AGENTS_DIRNAME) as agents_fd:
            yield agents_fd

    def ensure_directory_structure(self) -> None:
        """Create base directories for agents and workstreams.

        Each dir is owned by the agent uid because the daemon runs as
        root on the host; bind-mounted dirs would otherwise be
        root-owned and unwritable by the agent (uid 1000) inside
        the container. See ``src/_chown.py`` for the full rationale.

        The tree is agent-writable, so every directory is created and
        opened relative to a workspace descriptor without following a link
        and owned through ``fchown`` (SEC-1/WSD-3): a link a session
        planted is never chowned or written through. An entry a session can
        break (a link, a file, a permission it removed) is logged and
        skipped; an environmental failure (no space, I/O, read-only file
        system, exhausted descriptors) raises after the remaining
        directories. Only a workspace root that cannot be opened is fatal
        on its own.
        """
        failures = MaterializationFailures()
        with open_workspace_root(self._workspace) as workspace_fd:
            for parts in (
                (AGENTS_DIRNAME,),
                (AGENTS_DIRNAME, MANAGER_DIRNAME),
                ("workstreams",),
            ):
                try:
                    ensure_owned_directory(workspace_fd, *parts)
                except OSError as exc:
                    failures.handle(exc, str(self._workspace.joinpath(*parts)))
        failures.raise_if_any()

    def write_office_claude_md(self, config: dict) -> None:
        """Write the shared office-level CLAUDE.md.

        This file is auto-discovered by ALL agents (it's in the parent
        directory of each agent's working directory).  Contains only
        shared workspace conventions — no Manager-specific rules.
        """
        office_name = config.get("office_name", "Office")
        # Human output uses the shared platform contract, with no hidden style override.
        content = SHARED_OFFICE_CLAUDE_MD.format(
            office_name=office_name,
            office_specs_index=render_office_specs_index(
                config.get("specs", []),
            ),
        )
        with open_workspace_root(self._workspace) as workspace_fd:
            try:
                atomic_replace_file(workspace_fd, CLAUDE_MD_FILENAME, content)
            except OSError as exc:
                if is_environmental_error(exc):
                    raise
                logger.warning(
                    "The shared office CLAUDE.md could not be written (%s); it "
                    "is skipped until the entry is fixed.",
                    exc,
                )
                return
        logger.info("Wrote shared office CLAUDE.md for '%s'", office_name)

    def write_manager_claude_md(self, config: dict) -> None:
        """Write the Manager-specific CLAUDE.md to agents/manager/.

        Auto-discovered when Manager runs from /workspace/agents/manager/.
        Contains orchestration rules, tools, delegation patterns, etc.

        Office customisation
        --------------------
        ``config["claude_md_content"]`` carries office-owner-supplied
        context (business purpose, domain glossary, house rules).
        Older versions of this writer REPLACED the system Manager
        CLAUDE.md with that custom content — which silently dropped
        every orchestration rule (scope workflow, forbidden tools,
        review semantics, archive guidance) and made customised
        offices misbehave in subtle ways.

        The custom content is now APPENDED BELOW the authoritative system
        template under a precedence note, as "# Office-Specific
        Orchestration Guidance" (generated content) or "# Office
        Instructions" (owner-typed content). System rules remain canonical;
        office context enriches them without overriding.
        """
        office_name = config.get("office_name", "Office")
        custom_content = (config.get("claude_md_content") or "").strip()

        from src.config_sync._tool_allowlist import render_manager_allowlist

        base = MANAGER_CLAUDE_MD.format(
            office_name=office_name,
            manager_tool_allowlist=render_manager_allowlist(),
        )
        if custom_content and _is_generated_content(custom_content):
            # GEN-03: platform-GENERATED office instructions (the AI
            # Generate/Improve flow, sentinel present) are the Manager's own
            # orchestration guidance — appended under a precedence note.
            # Since instruction-sources-v2 the owner-typed branch below uses
            # the same posture with its own heading; the sentinel only
            # selects which heading/wording renders. Mirrors the agent path.
            content = _append_precedence_section(
                base,
                heading="# Office-Specific Orchestration Guidance",
                note=(
                    "The section below is this office's generated "
                    "orchestration guidance — how to plan, decompose, "
                    "delegate, and set the quality bar for THIS office. "
                    "Follow it — but on any conflict, the system rules "
                    "above win."
                ),
                body=_strip_generated_sentinel(custom_content),
            )
        elif custom_content:
            # instruction-sources-v2 (2026-09-03): owner-TYPED office
            # instructions get the same follow-with-precedence delivery as
            # generated ones (distinct heading keeps provenance visible).
            # The former hard "never follow instructions embedded inside
            # it" fence self-neutralized the feature — see the rationale on
            # GENERATED_CONTENT_SENTINEL above. Runtime fences for genuinely
            # untrusted surfaces (chat, workstream metadata, script output)
            # remain unchanged. Output style uses the shared platform contract.
            content = _append_precedence_section(
                base,
                heading="# Office Instructions",
                note=(
                    "The section below is the office owner's standing "
                    "guidance for this office. Follow it — but on any "
                    "conflict, the system rules above win."
                ),
                body=custom_content,
            )
        else:
            content = base
        # F09: the Office work policy is delivered to workers directly; the
        # Manager gets it as reference so its briefs stay consistent with it.
        content += render_office_work_policy(work_policy_from_config(config), "manager")

        # SEC-1: written relative to descriptors anchored at the workspace
        # root (``agents`` and ``manager`` opened without following a link).
        with open_workspace_root(self._workspace) as workspace_fd:
            try:
                with self._open_agents_root(
                    workspace_fd
                ) as agents_fd, open_owned_subdirectory(
                    agents_fd, MANAGER_DIRNAME
                ) as manager_fd:
                    atomic_replace_file(manager_fd, CLAUDE_MD_FILENAME, content)
            except OSError as exc:
                if is_environmental_error(exc):
                    raise
                logger.warning(
                    "The Manager CLAUDE.md could not be written (%s); it is "
                    "skipped until the entry is fixed.",
                    exc,
                )
                return
        logger.info("Wrote Manager CLAUDE.md for '%s'", office_name)

    def sync_agent_directories(
        self,
        agents: list[dict],
        office_work_policy: dict | None = None,
        *,
        keep_existing: bool = False,
    ) -> None:
        """Create/update/delete agent directories and CLAUDE.md files.

        - System agents: ALWAYS overwritten from SYSTEM_AGENT_CLAUDE_MD constants.
        - Custom agents: use claude_md_content if set, else generate from config.
        - ``office_work_policy`` (F09): the live Office work policy, appended
          to EVERY agent directory — system, custom and consult agents (the
          Planner applies it while writing briefs). It is an office-level
          layer below platform rules, not a system-agent customisation.
          ``None``/blank appends nothing (byte-identical output).
        - Orphan directories (agents no longer in config) are removed.
        - The 'manager' directory is never cleaned up (handled separately).
        - ``keep_existing`` (summary-shaped roster, X53): an agent whose
          CLAUDE.md already exists keeps it; only missing files are written.
        """
        policy_section = render_office_work_policy(office_work_policy, "worker")
        agents_dir = self._workspace / AGENTS_DIRNAME
        seen_names: set[str] = {MANAGER_DIRNAME, ".instances"}

        with ExitStack() as stack:
            # SEC-1: every agent directory write, ownership change and orphan
            # removal runs relative to a descriptor of ``agents/`` opened from
            # the workspace root without following a link. The tree is
            # agent-writable and the daemon may run as root: a session that
            # swaps ``agents`` (or an agent directory, ``.claude``, a
            # ``CLAUDE.md``) for a link must never redirect a root-owned
            # write, chown or ``rmtree`` outside the workspace.
            workspace_fd = stack.enter_context(open_workspace_root(self._workspace))
            try:
                agents_fd = stack.enter_context(self._open_agents_root(workspace_fd))
            except OSError as exc:
                if is_environmental_error(exc):
                    raise
                logger.error(
                    "%s could not be opened as a real directory (%s); refusing "
                    "to write agent directories through it.",
                    agents_dir,
                    exc,
                )
                return

            # A session-caused failure is skipped; an environmental one is
            # raised after
            # every other agent directory is written.
            failures = MaterializationFailures()
            written = 0
            for agent in agents:
                name = agent.get("name", "")
                if not name:
                    continue
                seen_names.add(name)

                # 07/H-13: a name must be one plain directory entry.
                if not is_safe_agent_name(name):
                    logger.warning(
                        "Skipping agent with unsafe name %r — it would resolve "
                        "outside the workspace agents directory", name,
                    )
                    continue
                # One agent's directory must not stop the others.
                try:
                    with open_owned_subdirectory(agents_fd, name) as agent_fd:
                        if not (
                            keep_existing
                            and is_regular_file(agent_fd, CLAUDE_MD_FILENAME)
                        ):
                            content = self._get_agent_claude_md(agent) + policy_section
                            atomic_replace_file(agent_fd, CLAUDE_MD_FILENAME, content)
                        # Wire the PreToolUse Bash guard for this agent's sessions.
                        _write_hook_settings_in(agent_fd, name)
                    written += 1
                except OSError as exc:
                    failures.handle(exc, f"agent directory {name}")

            self._remove_orphan_agent_directories(
                agents_fd, agents_dir, seen_names, failures
            )

        if written:
            logger.info("Synced %d agent CLAUDE.md files", written)
        failures.raise_if_any()

    @staticmethod
    def _remove_orphan_agent_directories(
        agents_fd: int,
        agents_dir: Path,
        seen_names: set[str],
        failures: MaterializationFailures,
    ) -> None:
        """Remove real agent directories no agent in this sync names.

        Only entries that are directories themselves are considered (a link
        is never followed or removed as a directory); each removal walks by
        descriptor, so nothing outside ``agents/`` can be reached.
        """
        # An entry whose type cannot be read (a session removed search
        # permission on ``agents/``) is never removed, but counts as an
        # existing directory for the guard below.
        real_directories, unknown = list_real_directories(
            agents_fd, failures, str(agents_dir)
        )
        # CTX-03: empty-sync guard (mirrors ScriptSyncer). A transient backend
        # error at daemon start degrades the agents list to [] (handlers.py),
        # and without this guard we'd rmtree EVERY agent dir — playbooks,
        # per-agent .claude/settings.json hook files, the lot. ``seen_names``
        # always contains "manager", so "only manager" means "no real agents
        # in this sync" → refuse orphan cleanup.
        real_incoming = seen_names - {MANAGER_DIRNAME, ".instances"}
        has_existing = any(
            name != MANAGER_DIRNAME for name in (*real_directories, *unknown)
        )
        if not real_incoming and has_existing:
            logger.warning(
                "Sync returned 0 agents but %s has agent directories. "
                "Refusing orphan cleanup — assuming a transient backend error. "
                "Restart cbcl after a real 'all agents deleted' to re-trigger.",
                agents_dir,
            )
            return
        for name in real_directories:
            if name in seen_names:
                continue
            try:
                remove_directory_tree(agents_fd, name)
            except OSError as exc:
                # Leaving an orphan in place is harmless; a session can make
                # its removal fail forever (a read-only or very deep tree).
                failures.skip(exc, f"orphan agent directory {name} (removal)")
                continue
            logger.info("Removed orphan agent directory: %s", name)

    def sync_workstream_directories(self, workstreams: list[dict]) -> None:
        """Create/update/move workstream directories and their CLAUDE.md files.

        Directories follow ``workstream_dir_slug(name, short_code)``. A
        workstream-id -> directory map (``.cubicle/workstream_dirs.json``)
        turns a rename into a MOVE of the old directory, so task outputs,
        the approved spec, intake records and learnings follow the
        workstream (X46). Orphans are deleted only when they hold nothing
        but ``CLAUDE.md``; everything else is archived, never destroyed.
        See ``workstream_dirs`` for the move/merge/archive rules.

        Raises ``OSError`` (after the whole pass) when the directory map or
        a workstream CLAUDE.md cannot be saved for an environmental reason
        (a full disk, an I/O error; at once when ``workstreams/`` itself
        cannot be opened for one): worker admission stays closed until the
        moves and instructions are written. An error a session can cause at
        one directory is logged and that directory skipped.
        """
        ws_dir = self._workspace / "workstreams"
        failures = MaterializationFailures()
        layout = WorkstreamLayout(self._workspace, failures)
        try:
            layout.load()
        except Exception:
            logger.exception("Could not read the workstream directory map.")

        entries: list[tuple[dict, str | None, str]] = []
        seen_slugs: set[str] = set()
        for ws in workstreams:
            if not ws.get("name"):
                continue
            slug = layout.directory_for(ws)
            if slug in seen_slugs:
                # Legacy duplicates (the backend now refuses new ones) share
                # one directory; the last writer's CLAUDE.md wins, as before.
                logger.warning(
                    "Workstreams %r share the workspace directory %r; rename "
                    "one so each keeps its own CLAUDE.md and spec.md.",
                    ws.get("name"),
                    slug,
                )
            seen_slugs.add(slug)
            entries.append((ws, canonical_workstream_id(ws.get("id")), slug))

        with ExitStack() as stack:
            # CM4: every workstream write runs relative to a descriptor of
            # ``workstreams/`` opened without following a link, so a link
            # swapped in by a session (the tree is agent-writable; the
            # daemon may run as root) is never written or chowned through.
            try:
                root_fd = stack.enter_context(open_workstreams_root(self._workspace))
            except OSError as exc:
                # B4-bugs-1: no space, I/O or exhausted descriptors must keep
                # admission closed and retry, as for the agent directories.
                if is_environmental_error(exc):
                    raise
                logger.error(
                    "%s is not a real directory (%s); refusing to write "
                    "workstream directories through it.",
                    ws_dir,
                    exc,
                )
                return

            # CTX-03: empty-sync guard — a transient backend error degrades
            # the workstream list to [], and an unguarded sweep would archive
            # every workstream dir (and a staged rename). With no workstream
            # to write, touch neither the directories nor the identity map.
            if not seen_slugs:
                if has_real_directories(root_fd):
                    logger.warning(
                        "Sync returned 0 workstreams but %s has workstream "
                        "directories. Refusing orphan cleanup — assuming a "
                        "transient backend error. Protects task outputs and "
                        "spec.md files.",
                        ws_dir,
                    )
                return

            # Any failure here (the tree and the map are agent-writable) must
            # not stop the rest of the sync — CLAUDE.md files, agent
            # directories and task admission depend on it completing.
            try:
                layout.prepare(root_fd, entries)
                layout_ready = True
            except Exception:
                logger.exception(
                    "Could not reconcile workstream directories; renamed "
                    "directories are left in place and retried on the next sync."
                )
                layout_ready = False

            written = 0
            blocked = layout.blocked if layout_ready else set()
            freed = layout.freed if layout_ready else set()
            for ws, _ws_id, slug in entries:
                if slug in blocked:
                    # A deleted workstream's directory that could not be
                    # archived yet, or a directory whose move is held: this
                    # workstream's CLAUDE.md must not land in it. A name this
                    # pass freed is still recorded as the workstream's (the
                    # final save does), so a lost save keeps it attributed:
                    # workers write to the declared directory meanwhile.
                    if slug in freed:
                        layout.record_placement(_ws_id, slug)
                    logger.warning(
                        "Workstream directory %s is still in use by another "
                        "(or a deleted) workstream; its CLAUDE.md is written "
                        "on a later sync.",
                        slug,
                    )
                    continue
                # CM1: an error a session can cause at one workstream's
                # directory (a file or link at its name, a permission) is
                # logged and skipped, never a sync failure that would pause
                # worker admission. An environmental one (no space, I/O) is
                # recorded and raised after the pass, so the sync is retried
                # and admission stays closed meanwhile (B4-bugs-1).
                try:
                    if layout_ready and not layout.record_placement(_ws_id, slug):
                        # The map cannot record the layout first: no
                        # directory it could not attribute is laid out.
                        continue
                    write_workstream_claude_md(
                        root_fd, slug, generate_workstream_claude_md(ws)
                    )
                    written += 1
                except OSError as exc:
                    failures.handle(exc, f"workstream directory {slug} CLAUDE.md")
                except Exception:
                    logger.exception(
                        "Could not write the CLAUDE.md of workstream directory "
                        "%s; it is skipped this sync.",
                        slug,
                    )

            # MV2: while the backend is rolled back, a workstream kept in the
            # declared layout is still addressed through its legacy directory
            # (task payloads carry no ``workspace_dir``): its workers and the
            # Planner read CLAUDE.md there, so it is written there as well.
            rows = {ws_id: ws for ws, ws_id, _slug in entries if ws_id}
            for legacy, ws_id in layout.kept_legacy_targets(seen_slugs).items():
                if legacy in blocked or ws_id not in rows:
                    continue
                try:
                    write_workstream_claude_md(
                        root_fd, legacy, generate_workstream_claude_md(rows[ws_id])
                    )
                except OSError as exc:
                    failures.handle(
                        exc, f"legacy workstream directory {legacy} CLAUDE.md"
                    )
                except Exception:
                    logger.exception(
                        "Could not write the CLAUDE.md of legacy workstream "
                        "directory %s.",
                        legacy,
                    )
            # A legacy directory that now has several kept workstreams keeps
            # no CLAUDE.md: one written there while it had a single owner
            # would give the others' workers that workstream's instructions.
            for legacy in sorted(layout.shared_kept_targets):
                if legacy in blocked or legacy in seen_slugs:
                    continue
                try:
                    if remove_workstream_claude_md(root_fd, legacy):
                        logger.info(
                            "Removed the CLAUDE.md of legacy workstream "
                            "directory %s (several workstreams resolve to it).",
                            legacy,
                        )
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(
                        "Could not remove the CLAUDE.md of legacy workstream "
                        "directory %s (%s).",
                        legacy,
                        exc,
                    )

            if layout_ready:
                try:
                    layout.finish(root_fd, seen_slugs)
                except Exception:
                    logger.exception(
                        "Could not clean up orphan workstream directories; "
                        "they are kept and retried on the next sync."
                    )

        if written:
            logger.info("Synced %d workstream CLAUDE.md files", written)
        failures.raise_if_any()

    @staticmethod
    def compose_task_agent_claude_md(profile: dict) -> str:
        """The task-Agent CLAUDE.md for a retained Profile snapshot.

        The CURRENT platform playbook for the Profile, the retained-
        configuration note (above any Office Notes, X51), the Profile-owned
        office notes, and the Office work policy PINNED in the snapshot
        (F09). ``agent_instance_workspace`` renders this fresh on every
        attempt from the archived Profile-owned parts, so platform rules
        follow the running daemon while the Profile's own content stays
        pinned. A snapshot from before F09 (no pinned key) gets no policy.
        """
        return ClaudeMdWriter._get_agent_claude_md(
            profile, retained_note=RETAINED_TASK_AGENT_NOTE
        ) + render_pinned_work_policy(profile)

    @staticmethod
    def _get_agent_claude_md(agent: dict, *, retained_note: str = "") -> str:
        """Get the CLAUDE.md content for an agent.

        System agents: always use the platform-owned template (the
        per-role CLAUDE.md). Those are the source of truth and must
        not be customised by office owners.

        Custom agents: compose the generator's baseline (role
        signature + SHARED_AGENT_WORK_RULES + completion block) and,
        if ``claude_md_content`` is set, append it as an enrichment
        section. Earlier behaviour REPLACED the generated baseline
        with the user string and silently lost the delivery / tool
        error / reviewer guidance — this caused custom agents to
        routinely fail to register deliverables and to misinterpret
        tool errors as server outages. The enrichment model keeps
        the baseline authoritative and lets office owners layer
        project-specific rules on top.
        """
        name = agent.get("name", "")
        agent_type = agent.get("agent_type", "custom")

        if agent_type == "system" and name in SYSTEM_AGENT_CLAUDE_MD:
            base = SYSTEM_AGENT_CLAUDE_MD[name]
        else:
            base = generate_custom_agent_claude_md(agent)

        # CTX-02: the SSH / office-secrets-in-shell / direct-git guidance is
        # follows the Profile's intended Bash workflow. Omitting this prose
        # reduces irrelevant context; it does not disable native CLI tools.
        # Profile allowed_tools is guidance, not an execution boundary.
        # X05: the consult-only agents get the variant that names only tools
        # their catalogs hold (no escalate_blocker; no list_office_secrets
        # for the Flow Architect / Data Curator).
        allowed_tools = agent.get("allowed_tools") or []
        if "Bash" in allowed_tools:
            base = base + "\n\n" + bash_capability_rules_for(name)

        # Static "Helpers (Subagents)" were removed: agents work alone by
        # default, and the single orchestration path is now ``ultracode``
        # (Claude Code dynamic workflows) — model-driven, so it needs no
        # static CLAUDE.md subagent menu and no ``--agents`` definitions.
        # Non-ultracode workers run with the Agent/Task spawn tools disallowed
        # (``_session_policy.build_session_policy``), so advertising subagents
        # here would point the agent at a tool it can't call. No subagents
        # section is emitted; its builder was deleted (2026-08-13) rather
        # than kept for a revival that had not come in two months — git
        # history holds it if the Helpers feature returns.
        # ``retained_note`` (task Agents only) belongs to the platform part:
        # it precedes the office-authored section so the "system rules above
        # win" precedence note covers it (X51).
        platform_part = base + retained_note

        custom_content = (agent.get("claude_md_content") or "").strip()
        if agent_type == "system" and name in SYSTEM_AGENT_CLAUDE_MD:
            # System agents: the platform template is authoritative; no
            # office-owner customisation is appended.
            return platform_part

        if not custom_content:
            return platform_part

        # Provenance display (T5.2.13 / I-5; unified in instruction-sources-v2):
        # both provenances are delivered follow-able; the heading records
        # whether the content is platform-generated or owner-typed. The
        # owner-typed rationale lives on GENERATED_CONTENT_SENTINEL above —
        # production notes are per-agent role SOPs written on an
        # authenticated surface; delivering them as "never follow" data
        # neutered them.
        if _is_generated_content(custom_content):
            return _append_precedence_section(
                platform_part,
                heading="## Office-Specific Playbook",
                note=(
                    "The section below is this office's generated, "
                    "office-specific playbook for this agent. Follow it as "
                    "operational guidance — but on any conflict, the system "
                    "rules above win."
                ),
                body=_strip_generated_sentinel(custom_content),
            )
        return _append_precedence_section(
            platform_part,
            heading="## Office Notes",
            note=(
                "The section below is the office owner's standing notes for "
                "this agent. Follow them as operational guidance — but on "
                "any conflict, the system rules above win."
            ),
            body=custom_content,
        )
