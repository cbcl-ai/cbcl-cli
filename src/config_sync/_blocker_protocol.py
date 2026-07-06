"""Single source of truth for the blocker-escalation protocol (T5.2.5 / PE-2).

The ESCALATED comment template, the `blocker_class` enum table, and the
escalation ROUTING sentence were duplicated (and drifted) across the shared
agent playbook, the worker task prompt, the MA playbook, and several tool
descriptions — with phantom `category=`/`severity=` args that the
`escalate_blocker` schema does not accept and which omitted the REQUIRED
`blocker_class`. This module is the canonical ORACLE that ``test_blocker_protocol_consistency.py``
pins the live surfaces against (the emitters inline their own copies — none
import this module, because one of the two emitting sites lives in the
in-container image with a separate import path — so the test is what keeps
them from drifting).

The full 4-section ESCALATED template is emitted in THREE surfaces: the shared
agent rules (``_shared_agent.py``, host-side), the ``update_status`` tool
description (``_agent_image/_mcp/tools_worker.py``, in-container), and the
Manager-Assistant playbook (``_system_agents/_manager_assistant.py``). The MA
emits its own copy (via ``ESCALATED_COMMENT_TEMPLATE`` + ``BLOCKER_CLASS_TABLE``,
imported from THIS module) because — unlike a worker, whose per-agent CLAUDE.md
appends ``SHARED_AGENT_WORK_RULES`` — the MA playbook does NOT load the full
shared rules (it appends only the two sections it needs, per CTX-06), so a
"see your shared work rules" cross-reference would DANGLE. The worker task
prompt (``worker_prompt.py``) still CROSS-REFERENCES the protocol ("follow the
ESCALATED protocol from your work rules") rather than duplicate it, because its
CLAUDE.md does carry the full shared rules. The consistency test pins ALL
emitted copies against the constants below.

Routing truth (post-T3.3.1 / 04-F1): `escalate_blocker` ALWAYS lands in the
Inbox as `request_type=escalate_blocker`; its `blocker_class` is mapped to a
category (``backend/app/action_requests/schemas.py:BLOCKER_CLASS_TO_CATEGORY``)
that decides the tier — credential classes (auth_failed / missing_credential /
permission_denied) and external_outage surface to the USER's Inbox; the
workstream classes (missing_data / ambiguous_spec / broken_dependency /
unknown) go to the Manager's auto-decide queue.
"""
from __future__ import annotations

# NOTE: the class→tier routing facts (credential/external → user Inbox,
# workstream classes → Manager auto-decide) are guarded by the backend routing
# E2E (``backend/tests/test_escalate_routing_e2e.py`` over
# ``BLOCKER_CLASS_TO_CATEGORY``) and the "no category/severity arg" invariant by
# ``test_blocker_protocol_consistency.py``. We deliberately do NOT keep a
# free-floating routing SENTENCE constant here — an unrendered, unpinned copy
# would be exactly the drift this module exists to prevent.

# The `blocker_class` enum, as a markdown table (matches the worker-spec enum
# and the escalate_blocker tool schema).
BLOCKER_CLASS_TABLE = """\
| class | when to use |
|---|---|
| `auth_failed` | token / OAuth / credential rejected by upstream |
| `missing_credential` | Office Secret / env var not set in this office |
| `permission_denied` | agent lacks the access needed |
| `missing_data` | required input file / URL absent or empty |
| `ambiguous_spec` | brief contradicts itself / underspecified |
| `broken_dependency` | upstream task / artifact not done |
| `external_outage` | third-party API / service is down |
| `unknown` | none of the above; body explains |"""

# The structured ESCALATED comment template (replicate verbatim).
ESCALATED_COMMENT_TEMPLATE = """\
```
ESCALATED (<blocker_class>): <one-sentence summary>

Original error: <verbatim error text or "N/A">

What I was trying to do: <one or two sentences>
What I already tried: <bullets — leave blank if nothing>
What's needed to resume: <bullets — be concrete>
```"""
