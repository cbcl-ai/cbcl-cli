"""L14: the agent image ships what the Anthropic document skills run.

The catalog offers Anthropic's ``docx``, ``pdf``, ``pptx`` and ``xlsx``
skills. Their scripts import Python packages and call LibreOffice, pandoc,
qpdf, tesseract and two global npm modules (each skill's SKILL.md
"Dependencies" section). The office container runs as the non-root ``agent``
user, so nothing can be added at run time: the image itself must carry them,
pinned, without AGPL software (Ghostscript, PyMuPDF) and without the
Anthropic API client (Cubicle is subscription-only). The same holds for
slack-gif-creator's imageio; webapp-testing's Playwright and browser are
deliberately left out and stay flagged for the generator.
"""

from __future__ import annotations

import re
import shlex

from src._setup_prompts import _CATALOG_RUNTIME_PACKAGES
from src.docker.container_manager import _DOCKER_DIR

_DOCUMENT_SKILLS = ("anthropic-docx", "anthropic-pdf", "anthropic-pptx", "anthropic-xlsx")


def _instructions() -> list[str]:
    """Dockerfile instructions with ``\\`` continuations joined."""
    text = (_DOCKER_DIR / "Dockerfile.agent").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"\\\n", " ", "\n".join(lines)).splitlines()


def _installed(command: str) -> set[str]:
    """Arguments of every ``<command>`` in a RUN step, options excluded."""
    found: set[str] = set()
    for instruction in _instructions():
        if not instruction.startswith("RUN "):
            continue
        for step in instruction[4:].split("&&"):
            words = shlex.split(step)
            prefix = shlex.split(command)
            if words[: len(prefix)] == prefix:
                found.update(word for word in words[len(prefix) :] if not word.startswith("-"))
    return found


def test_document_skill_system_tools_are_installed() -> None:
    apt = _installed("apt-get install")
    for package in (
        "libreoffice-writer-nogui",
        "libreoffice-calc-nogui",
        "libreoffice-impress-nogui",
        "pandoc",
        "qpdf",
        "tesseract-ocr",
        "tesseract-ocr-eng",
        "poppler-utils",
        "fonts-liberation",
        "fonts-crosextra-carlito",
        "fonts-crosextra-caladea",
        "fonts-dejavu-core",
    ):
        assert package in apt, package
    # Every apt step skips recommends, which keeps optional (and possibly
    # AGPL) extras such as Ghostscript out of the image.
    for instruction in _instructions():
        if "apt-get install" in instruction:
            assert instruction.count("apt-get install") == instruction.count(
                "--no-install-recommends"
            ), instruction


def _pinned_pip() -> dict[str, str]:
    """``{package: requirement}`` for every ``pip install`` argument."""
    return {
        re.sub(r"\[.*\]", "", requirement).split("==")[0].lower(): requirement
        for requirement in _installed("pip install")
    }


def test_document_skill_python_packages_are_pinned() -> None:
    pinned = _pinned_pip()
    for package in (
        "openpyxl",
        "pandas",
        "markitdown",
        "pypdf",
        "pdfplumber",
        "reportlab",
        "pytesseract",
        "pdf2image",
        "pypdfium2",
        "pillow",
        "defusedxml",
        "lxml",
    ):
        assert package in pinned, package
        assert re.fullmatch(r"[\w.\-\[\],]+==[\w.]+", pinned[package]), pinned[package]
    assert "pptx" in pinned["markitdown"] and "xlsx" in pinned["markitdown"]


def test_document_skill_node_modules_are_global_pinned_and_resolvable() -> None:
    modules = _installed("npm install")
    assert any(re.fullmatch(r"docx@\d+\.\d+\.\d+", name) for name in modules), modules
    assert any(re.fullmatch(r"pptxgenjs@\d+\.\d+\.\d+", name) for name in modules), modules
    assert any(
        re.search(r"npm install (\S+ )*-g\b", instruction) for instruction in _instructions()
    )
    # Node does not search the global folder by itself: ``require('docx')``
    # from a task's working directory needs NODE_PATH.
    assert "ENV NODE_PATH=/usr/lib/node_modules" in _instructions()


def test_no_agpl_software_or_anthropic_api_client() -> None:
    installed = {
        re.sub(r"[\[=@].*", "", name).lower()
        for command in ("apt-get install", "pip install", "npm install")
        for name in _installed(command)
    }
    for forbidden in ("ghostscript", "gs", "pymupdf", "fitz", "anthropic"):
        assert forbidden not in installed, forbidden


def test_installed_document_skills_are_not_flagged_as_missing_packages() -> None:
    for template_id in _DOCUMENT_SKILLS:
        assert template_id not in _CATALOG_RUNTIME_PACKAGES, template_id


def test_slack_gif_runtime_is_installed_and_webapp_testing_stays_flagged() -> None:
    """slack-gif-creator's ``core/gif_builder.py`` imports ``imageio.v3``
    (numpy and Pillow come with the document layer), so it is installed and
    unflagged. webapp-testing's Playwright and Chromium are not installed and
    stay flagged."""
    pinned = _pinned_pip()
    assert re.fullmatch(r"imageio==[\w.]+", pinned.get("imageio", "")), pinned
    assert "anthropic-slack-gif-creator" not in _CATALOG_RUNTIME_PACKAGES
    installed = {
        re.sub(r"[\[=@].*", "", name).lower()
        for command in ("apt-get install", "pip install", "npm install")
        for name in _installed(command)
    }
    assert "playwright" not in installed
    assert "Playwright" in _CATALOG_RUNTIME_PACKAGES["anthropic-webapp-testing"]


def test_web_artifacts_builder_is_flagged_and_mcp_builder_is_not() -> None:
    """f4 item 4: web-artifacts-builder's init script runs
    ``npm install -g pnpm``, which fails as the non-root agent user; the image
    ships no pnpm, so the skill is flagged. mcp-builder's build workflow runs;
    only its optional evaluation runner needs the Anthropic SDK (not
    installed, subscription-only), so it stays unflagged."""
    installed = {
        re.sub(r"[\[=@].*", "", name).lower()
        for command in ("apt-get install", "pip install", "npm install")
        for name in _installed(command)
    }
    assert "pnpm" not in installed
    assert "pnpm" in _CATALOG_RUNTIME_PACKAGES["anthropic-web-artifacts-builder"]
    assert "anthropic-mcp-builder" not in _CATALOG_RUNTIME_PACKAGES
