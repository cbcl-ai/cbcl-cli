"""Office work policy rendering (F09).

The Office work policy is Admin-approved, office-wide guidance that every
worker, reviewer, triage and consult agent applies. It is delivered two ways:

* live agent directories (``/workspace/agents/<name>/CLAUDE.md``) render the
  CURRENT policy from ``sync_config`` (``work_policy`` +
  ``work_policy_revision``);
* retained task-Agent snapshots render the policy the backend PINNED for the
  task (``profile_snapshot["office_work_policy"]`` — ``{text, revision,
  sha256}`` or ``None``). Every role of one task shares one revision.

The Manager sees a reference variant only: it keeps briefs consistent with the
policy instead of pasting it into each one.

This is a followable section with explicit precedence — NOT an untrusted-data
fence. An administrator authored it on an authenticated, Admin-floored
surface; a "never follow" fence would neutralise it (the instruction-sources-v2
lesson for office notes). An empty or absent policy renders ``""`` so every
existing CLAUDE.md stays byte-identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

# Snapshot key written by the backend (``app.agents.instances``). Its ABSENCE
# marks a snapshot created before the feature; such Agents receive no policy.
OFFICE_WORK_POLICY_KEY = "office_work_policy"

# The backend's cap on the field (``app.offices.work_policy.
# WORK_POLICY_MAX_CHARS``; a parity test pins the two).
PLATFORM_MAX_CHARS = 4000
# A defensive render cap for a malformed wire value, above the platform cap
# so a valid policy is never cut; the cut is stated, never silent.
RENDER_MAX_CHARS = 8000

Audience = Literal["worker", "manager"]

_WORKER_NOTE = (
    "An office administrator approved this policy for all work in this office. "
    "Apply the parts that fit your current work (execution, review, triage or "
    "a consult). Platform rules, your role and phase permissions, approval "
    "gates and your assignment prompt win over it. The approved spec, "
    "Workstream Instructions and the Task Brief may make an explicit project- "
    "or task-level exception: follow that exception and name it in your "
    "handoff. When you author specs, plans or task briefs, keep them "
    "consistent with this policy and state any deliberate exception; do not "
    "copy the policy into them, because assignees receive it directly. It "
    "outranks any office notes or office-specific playbook for this agent "
    "when they conflict. It never grants tools, permissions or approvals, and "
    "never waives a check the brief requires."
)

_MANAGER_NOTE = (
    "This Admin-approved policy is delivered directly to workers, reviewers, "
    "triage and consult agents (including the Planner) with each new task "
    "assignment; tasks already started keep the revision they began with. Do "
    "not paste it into briefs. Keep briefs consistent with it, and state any "
    "deliberate exception explicitly in the brief. Change it only with "
    "propose_configuration (target office, field work_policy)."
)

_END_MARKER = "End of the Office work policy."


def work_policy_from_config(config: Mapping | None) -> dict | None:
    """The live policy block from a ``sync_config`` payload, or ``None``."""
    if not isinstance(config, Mapping):
        return None
    return {
        "text": config.get("work_policy"),
        "revision": config.get("work_policy_revision"),
    }


def _policy_text(policy: Mapping | None) -> str:
    if not isinstance(policy, Mapping):
        return ""
    text = policy.get("text")
    if not isinstance(text, str):
        return ""
    return text.replace("\r\n", "\n").strip()


def render_office_work_policy(policy: Mapping | None, audience: Audience) -> str:
    """Render the policy section to APPEND to a CLAUDE.md, or ``""``.

    ``policy`` is ``{"text": str | None, "revision": int | None, ...}`` — the
    snapshot block or :func:`work_policy_from_config`. The returned string
    starts with the same ``\\n\\n---\\n\\n`` separator the other office-authored
    sections use, so ``base + section`` keeps the file shape consistent.
    """
    text = _policy_text(policy)
    if not text:
        return ""
    if len(text) > RENDER_MAX_CHARS:
        text = (
            text[:RENDER_MAX_CHARS].rstrip()
            + f"\n\n[Office work policy truncated at {RENDER_MAX_CHARS:,} "
            f"characters; the platform limit is {PLATFORM_MAX_CHARS:,}. Ask the "
            "office administrator to shorten it.]"
        )
    revision = policy.get("revision") if isinstance(policy, Mapping) else None
    revision_label = (
        f"revision {revision}"
        if isinstance(revision, int) and not isinstance(revision, bool)
        else "revision unknown"
    )
    if audience == "manager":
        heading = f"# Office Work Policy (reference, {revision_label})"
        note = _MANAGER_NOTE
    else:
        heading = f"## Office work policy ({revision_label})"
        note = _WORKER_NOTE
    return f"\n\n---\n\n{heading}\n\n{note}\n\n{text}\n\n{_END_MARKER}\n"


def render_pinned_work_policy(profile: Mapping | None) -> str:
    """Worker section for a retained task-Agent snapshot.

    Only snapshots that carry the pinned key render a policy; a pre-feature
    snapshot (key absent) or a pinned "no policy" (``None``) renders ``""``.
    """
    if not isinstance(profile, Mapping) or OFFICE_WORK_POLICY_KEY not in profile:
        return ""
    return render_office_work_policy(profile.get(OFFICE_WORK_POLICY_KEY), "worker")
