"""Canonical system-agent roster for the setup wizard + tool descriptions
(T5.2.7 / 04-F14 / 06-I-4).

There are SIX system agents (the Planner shipped as the fifth; pivot-1 T1
added the Builder as the sixth). Hand-written copies of the roster have
drifted before ("four" survived in three places, and the wizard's
reserved-name guards omitted ``planner``, colliding on
``UNIQUE(office_id, name)`` at apply time) — hence this single source.

This module is the single source of truth for the slug set. The prose sites
fix their wording in place; a test (`test_system_agent_roster_parity.py`) pins
every site against ``SYSTEM_AGENT_SLUGS`` so adding/removing a system agent
fails loudly until every render site is updated. (Cross-repo parity with the
backend ``SYSTEM_AGENT_DEFAULTS`` is asserted there too when importable.)
"""
from __future__ import annotations

# The six system-agent slugs every office ships with (pivot-1 T1 added the
# Builder). Order = creation order in backend ``SYSTEM_AGENT_DEFAULTS``. The
# wizard prose render-sites in ``_setup_prompts.py`` are hand-authored;
# ``test_system_agent_roster_parity`` pins every site against this set so
# adding/removing a system agent fails loudly until each site is updated.
SYSTEM_AGENT_SLUGS: tuple[str, ...] = (
    "analyst",
    "automation-script-developer",
    "auditor",
    "builder",
    "manager-assistant",
    "planner",
)
