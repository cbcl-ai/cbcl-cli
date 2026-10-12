"""The source reader names what it did not read and reads more text formats.

WP5 (office-configuration readiness review §5): files past the document limit
are listed by path instead of disappearing behind one generic warning, an
archive's bytes are charged once, and common code/config formats are studied
as text. The protected-path, link and size rules are unchanged.
"""

import json
import re
import zipfile
from pathlib import Path

import pytest

from src._agent_image import generation_sources as sources
from src._agent_image.secure_files import SecureWorkspace


def _write_files(root, names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {name}")


def test_files_past_the_document_limit_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 2)
    _write_files(tmp_path, ["source/a.md", "source/b.md", "source/c.md", "source/d.md"])
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["source/"])
    read = [document["path"] for document in result["documents"]]
    assert read == ["source/a.md", "source/b.md"]
    assert result["skipped"] == ["source/c.md", "source/d.md"]
    assert result["skipped_total"] == 2
    # The skipped files were never opened.
    assert "content of source/c.md" not in json.dumps(result)
    # One warning, first in the list, with the count and the names.
    warning = result["warnings"][0]
    assert warning.startswith("2 files were not read because the 2-document limit")
    assert "source/c.md, source/d.md" in warning
    assert len(warning) <= sources.SKIPPED_WARNING_CHARACTERS
    assert not any("additional sources were omitted" in w for w in result["warnings"])


def test_archive_members_past_the_limit_are_named_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 2)
    with zipfile.ZipFile(tmp_path / "pack.zip", "w") as archive:
        archive.writestr("README.md", "read me first")
        archive.writestr("docs/rules.md", "the rules")
        archive.writestr("src/app.py", "print('app')")
        archive.writestr("src/lib/util.py", "print('util')")
        archive.writestr(".env", "SECRET=canary")
        archive.writestr("../escape.md", "escape canary")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["pack.zip"])
    assert [document["path"] for document in result["documents"]] == [
        "pack.zip!/README.md",
        "pack.zip!/docs/rules.md",
    ]
    assert result["skipped"] == ["pack.zip!/src/app.py", "pack.zip!/src/lib/util.py"]
    assert result["skipped_total"] == 2
    # Protected and unsafe entries are neither read nor named.
    encoded = json.dumps(result)
    assert "canary" not in encoded
    assert ".env" not in encoded and "escape" not in encoded
    assert "print(" not in encoded
    assert result["warnings"][0].startswith("2 files were not read")


def test_an_archive_past_the_limit_is_named_but_not_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 1)
    (tmp_path / "guide.md").write_text("guide")
    with zipfile.ZipFile(tmp_path / "later.zip", "w") as archive:
        archive.writestr("inside.md", "inside canary")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["guide.md", "later.zip"])
    assert [document["path"] for document in result["documents"]] == ["guide.md"]
    assert result["skipped"] == ["later.zip"]
    assert "inside" not in json.dumps(result)


def test_skipped_names_are_bounded_but_all_are_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 1)
    monkeypatch.setattr(sources, "MAX_SKIPPED_PATH_CHARACTERS", 30)
    names = [f"source/file-{index:02d}.md" for index in range(8)]
    _write_files(tmp_path, names)
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["source/"])
    assert result["skipped_total"] == 7
    assert sum(len(path) for path in result["skipped"]) <= 30
    assert result["skipped"] == ["source/file-01.md"]
    assert "7 files were not read" in result["warnings"][0]
    assert " and 6 more" in result["warnings"][0]


def test_skipped_warning_names_the_first_few_within_the_limit():
    long_paths = [f"source/{'deep/' * 12}file-{index}.py" for index in range(40)]
    warning = sources._skipped_warning(long_paths, 526)
    assert warning.startswith("526 files were not read")
    assert long_paths[0] in warning
    assert long_paths[-1] not in warning
    assert " more." in warning
    assert len(warning) <= sources.SKIPPED_WARNING_CHARACTERS


def test_skipped_warning_counts_when_no_name_fits():
    warning = sources._skipped_warning(["x" * 400], 1)
    assert warning.startswith("1 file was not read")
    assert "x" * 20 not in warning
    assert len(warning) <= sources.SKIPPED_WARNING_CHARACTERS


def test_skipped_warning_comes_first_past_other_warnings(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 21)
    # 21 unreadable files fill the reader's 20-warning list first.
    (tmp_path / "source").mkdir()
    for index in range(21):
        (tmp_path / f"source/sheet-{index:02d}.xlsx").write_bytes(b"PK\x03\x04binary")
    (tmp_path / "source/zz-last.xlsx").write_bytes(b"PK\x03\x04binary")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["source/"])
    assert result["skipped"] == ["source/zz-last.xlsx"]
    assert len(result["warnings"]) == 20
    assert result["warnings"][0].startswith("1 file was not read")


def test_an_archive_is_charged_once_not_twice(tmp_path, monkeypatch):
    """The compressed archive and its members used to both spend the budget."""
    with zipfile.ZipFile(tmp_path / "pack.zip", "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("a.md", "a" * 4000)
        archive.writestr("b.md", "b" * 4000)
    archive_size = (tmp_path / "pack.zip").stat().st_size
    # Room for the archive OR its members, not both.
    monkeypatch.setattr(sources, "MAX_TOTAL_BYTES", archive_size + 100)
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["pack.zip"])
    assert [document["path"] for document in result["documents"]] == [
        "pack.zip!/a.md",
        "pack.zip!/b.md",
    ]
    assert all(not document.get("unreadable") for document in result["documents"])


def test_an_archive_still_costs_at_least_its_compressed_size(tmp_path, monkeypatch):
    """An archive with nothing readable cannot be read for free."""
    with zipfile.ZipFile(tmp_path / "a-empty.zip", "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(".env", "SECRET=" + "y" * 3000)
    with zipfile.ZipFile(tmp_path / "b-docs.zip", "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("guide.md", "guide text")
    first = (tmp_path / "a-empty.zip").stat().st_size
    second = (tmp_path / "b-docs.zip").stat().st_size
    monkeypatch.setattr(sources, "MAX_TOTAL_BYTES", first + second - 1)
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["a-empty.zip", "b-docs.zip"])
    by_path = {document["path"]: document for document in result["documents"]}
    assert by_path["b-docs.zip"]["unreadable"]
    assert "guide text" not in json.dumps(result)


@pytest.mark.parametrize(
    "name",
    [
        "deploy.sh",
        "setup.bash",
        "profile.zsh",
        "main.go",
        "App.java",
        "Build.kt",
        "build.gradle.kts",
        "lib.rs",
        "task.rb",
        "index.php",
        "util.c",
        "util.h",
        "engine.cc",
        "engine.cpp",
        "engine.hpp",
        "Program.cs",
        "View.swift",
        "Job.scala",
        "Card.vue",
        "Card.svelte",
        "theme.scss",
        "theme.sass",
        "theme.less",
        "tool.cfg",
        "nginx.conf",
        "app.properties",
        "build.gradle",
        "page.mdx",
        "schema.graphql",
        "api.proto",
        "main.tf",
        "Dockerfile",
        "Makefile",
        "GNUmakefile",
        "Dockerfile.agent",
        "makefile.inc",
    ],
)
def test_common_code_and_config_files_are_read_as_text(tmp_path, name):
    (tmp_path / name).write_text(f"text of {name}")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, [name])
    document = result["documents"][0]
    assert document["content"] == f"text of {name}"
    assert not document.get("unreadable")
    assert not result["warnings"]


def test_a_code_suffix_with_binary_content_stays_unreadable(tmp_path):
    (tmp_path / "blob.go").write_bytes(b"package main\x00\x01\x02")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["blob.go"])
    assert result["documents"][0]["unreadable"]
    assert not result["documents"][0]["content"]


def test_env_files_are_never_read_or_named(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/.env").write_text("TOKEN=canary")
    (tmp_path / "source/.env.example").write_text("TOKEN=example-canary")
    (tmp_path / "source/.env.production").write_text("TOKEN=prod-canary")
    (tmp_path / "source/notes.md").write_text("notes")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["source/"])
    assert [document["path"] for document in result["documents"]] == ["source/notes.md"]
    encoded = json.dumps(result)
    assert "canary" not in encoded and ".env" not in encoded


def test_office_formats_remain_named_but_unread(tmp_path):
    (tmp_path / "plan.docx").write_bytes(b"PK\x03\x04docx")
    with SecureWorkspace(tmp_path) as workspace:
        result = sources.prepare_sources(workspace, ["plan.docx"])
    assert result["documents"][0]["unreadable"]
    assert result["skipped"] == [] and result["skipped_total"] == 0


def test_domain_doc_lists_exactly_the_formats_read_as_text():
    """The one per-suffix list outside the code is in the domain doc.

    The public setup guide names groups of formats only, so this is the
    list that must not drift: a suffix the reader reads but the doc omits,
    or one the doc promises and the reader does not read, fails here.
    """
    doc = (
        Path(__file__).resolve().parents[2]
        / "docs" / "02-domain" / "instructions-and-generation.md"
    )
    if not doc.exists():
        pytest.skip("docs tree not present (standalone communicator checkout)")
    text = doc.read_text()
    start = text.index("Text is read from these suffixes:")
    passage = text[start:text.index("a NUL byte", start)]
    words = [
        word
        for span in re.findall(r"`([^`]+)`", passage)
        for word in span.split()
    ]
    suffixes = {word for word in words if word.startswith(".")}
    names = {word.casefold() for word in words if not word.startswith(".")}

    assert suffixes == set(sources._TEXT_SUFFIXES)
    # A name with a dot (``Dockerfile.agent``) is the doc's example of a
    # read name with a suffix, not a name of its own.
    assert {name for name in names if "." not in name} == set(
        sources._TEXT_FILE_NAMES
    )


def test_the_wizard_copy_states_the_readers_limits():
    """The setup wizard lists the reader's limits in hand-written copy
    (DescribeStep.tsx). Each figure is checked against the constant it
    states, so a changed limit or a changed sentence fails here."""
    step = (
        Path(__file__).resolve().parents[2]
        / "frontend_v2.1" / "src" / "components" / "setup" / "DescribeStep.tsx"
    )
    if not step.exists():
        pytest.skip("frontend tree not present (standalone communicator checkout)")
    text = step.read_text()
    start = text.index('data-testid="source-reading-limits"')
    limits = " ".join(text[start:text.index("</ul>", start)].split())
    mib = 1024 * 1024

    def figure(pattern: str) -> int:
        found = re.findall(pattern, limits)
        assert len(found) == 1, f"{pattern!r} not stated once in: {limits}"
        return int(found[0].replace(",", ""))

    assert figure(r"At most ([\d,]+) files are read") == sources.MAX_DOCUMENTS
    assert figure(r"([\d,]+) MB in total") * mib == sources.MAX_TOTAL_BYTES
    assert figure(r"file or ZIP over ([\d,]+) MB") * mib == sources.MAX_FILE_BYTES
    assert figure(r"more than ([\d,]+) files") == sources.MAX_ARCHIVE_FILES
    assert figure(r"([\d,]+) MB of contents") * mib == sources.MAX_ARCHIVE_BYTES
