"""Canonical system-agent roster for the setup wizard + tool descriptions
(T5.2.7 / 04-F14 / 06-I-4).

There are FIVE system agents, not four — the Planner shipped as the fifth.
Five hand-written copies of the roster drifted: three still said "four", and
the wizard's reserved-name guards omitted ``planner``, so a generated roster
could collide with the Planner system agent on ``UNIQUE(office_id, name)`` at
apply time.

This module is the single source of truth for the slug set. The prose sites
fix their wording in place; a test (`test_system_agent_roster_parity.py`) pins
every site against ``SYSTEM_AGENT_SLUGS`` so adding/removing a system agent
fails loudly until every render site is updated. (Cross-repo parity with the
backend ``SYSTEM_AGENT_DEFAULTS`` is asserted there too when importable.)
"""
from __future__ import annotations

# The five system-agent slugs every office ships with. Order = creation order
# in backend ``SYSTEM_AGENT_DEFAULTS``. The wizard prose render-sites in
# ``_setup_prompts.py`` are hand-authored; ``test_system_agent_roster_parity``
# pins every site against this set so adding/removing a system agent fails
# loudly until each site is updated.
SYSTEM_AGENT_SLUGS: tuple[str, ...] = (
    "analyst",
    "automation-script-developer",
    "auditor",
    "manager-assistant",
    "planner",
)
