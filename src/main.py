"""Cubicle Communicator CLI — bridge between platform and local AI execution."""

from __future__ import annotations

import click


@click.group()
def cli() -> None:
    """Cubicle — Claude Communicator"""


# Import commands to register them with the cli group.
# The import must happen after `cli` is defined so that cli_commands
# can import `cli` from this module and attach @cli.command() decorators.
import src.cli_commands  # noqa: E402, F401
