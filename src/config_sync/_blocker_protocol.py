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

The full 4-section ESCALATED template is emitted in exactly TWO surfaces
(the T5.3.4 target): the shared agent rules (``_shared_agent.py``, host-side)
and the ``update_status`` tool description (``_agent_image/_mcp/tools_worker.py``,
in-container). The worker task prompt (``worker_prompt.py``) and the MA playbook
CROSS-REFERENCE the protocol ("follow the ESCALATED protocol from your work
rules") rather than duplicate it. The consistency test pins BOTH emitted copies
against the constants below.

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
