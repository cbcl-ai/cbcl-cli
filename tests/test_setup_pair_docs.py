"""The public "Setup and pair" page names the options ``cbcl setup`` has.

The page is deployed separately from cbcl, so nothing else notices when
the two drift: it once documented ``cbcl setup --profile`` and
``cbcl setup --platform-url``, which never existed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import cli_commands

SETUP_PAGE = (
    Path(__file__).resolve().parents[2]
    / "landing_v2" / "content" / "docs" / "cbcl" / "setup-and-pair.mdx"
)


@pytest.fixture
def page() -> str:
    if not SETUP_PAGE.is_file():
        pytest.skip("landing docs not present (standalone communicator checkout)")
    return SETUP_PAGE.read_text()


def _setup_options() -> tuple[set[str], set[str]]:
    """The option names and the environment variables of ``cbcl setup``."""
    names: set[str] = set()
    variables: set[str] = set()
    for param in cli_commands.setup.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
        if isinstance(param.envvar, str):
            variables.add(param.envvar)
        elif param.envvar:
            variables.update(param.envvar)
    return names, variables


def _code_snippets(page: str) -> list[str]:
    """Every line of a code block and every inline code span of the page."""
    snippets: list[str] = []
    in_block = False
    for line in page.splitlines():
        if line.lstrip().startswith("```"):
            in_block = not in_block
        elif in_block:
            snippets.append(line)
        else:
            snippets.extend(re.findall(r"`([^`]+)`", line))
    return snippets


def test_page_names_every_option_of_cbcl_setup(page: str) -> None:
    names, variables = _setup_options()
    assert {"--company-token", "--non-interactive"} <= names
    assert "CBCL_COMPANY_TOKEN" in variables

    missing = sorted(name for name in names | variables if f"`{name}" not in page)
    assert not missing, f"the Setup and pair page does not name: {missing}"


def test_page_shows_no_option_cbcl_setup_lacks(page: str) -> None:
    names, _variables = _setup_options()

    shown: set[str] = set()
    for snippet in _code_snippets(page):
        _before, command, arguments = snippet.partition("cbcl setup")
        if command:
            shown.update(re.findall(r"(?<!\S)-{1,2}[a-z][a-z-]*", arguments))

    assert shown, "the page shows no cbcl setup command with an option"
    assert shown <= names, (
        f"the Setup and pair page shows options cbcl setup does not have: "
        f"{sorted(shown - names)}"
    )
