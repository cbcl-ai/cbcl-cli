"""The source reader keeps what it can read when parts of a source fail.

A corrupt, password-protected or unsupported ZIP entry is left out and named
in one warning per archive, without taking a document slot; an archive over a
traversal cap says which cap; the reader's time limit stops the run with one
honest warning instead of reporting every remaining file as missing. These
tests use an in-memory workspace, so they run on any development machine (the
protected reader itself needs Linux /proc).
"""

from __future__ import annotations

import io
import json
import time
import types
import zipfile

import pytest

from src import _source_survey, _source_warnings
from src._agent_image import generation_sources as sources


class _Workspace:
    """The two SecureWorkspace calls the reader makes, over a dict of files."""

    def __init__(self, files: dict[str, bytes], *, time_limit_after: int | None = None):
        self.files = files
        self.reads = 0
        self.time_limit_after = time_limit_after

    def list_files(self, path: str) -> list[str]:
        if path in self.files:
            raise NotADirectoryError(path)
        prefix = path.rstrip("/") + "/"
        names = sorted(name for name in self.files if name.startswith(prefix))
        if not names:
            raise FileNotFoundError(path)
        return names

    def read_bytes(self, path: str, limit: int) -> bytes:
        self.reads += 1
        if self.time_limit_after is not None and self.reads > self.time_limit_after:
            raise TimeoutError("reader deadline")
        data = self.files[path]
        if len(data) > limit:
            raise ValueError("File exceeds the read limit")
        return data


def _zip(members: dict[str, bytes], *, method: int = zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _set_method(archive: bytes, member: str, method: int) -> bytes:
    """Rewrite one entry's compression method in both of its headers."""
    data = bytearray(archive)
    name = member.encode()
    local = data.find(b"PK\x03\x04")
    while local != -1:
        length = int.from_bytes(data[local + 26 : local + 28], "little")
        if bytes(data[local + 30 : local + 30 + length]) == name:
            data[local + 8 : local + 10] = method.to_bytes(2, "little")
        local = data.find(b"PK\x03\x04", local + 4)
    central = data.find(b"PK\x01\x02")
    while central != -1:
        length = int.from_bytes(data[central + 28 : central + 30], "little")
        if bytes(data[central + 46 : central + 46 + length]) == name:
            data[central + 10 : central + 12] = method.to_bytes(2, "little")
        central = data.find(b"PK\x01\x02", central + 4)
    return bytes(data)


def _corrupt_member(archive: bytes, member: str) -> bytes:
    """Overwrite the middle of one deflated entry's compressed data."""
    data = bytearray(archive)
    name = member.encode()
    local = data.find(b"PK\x03\x04")
    while local != -1:
        length = int.from_bytes(data[local + 26 : local + 28], "little")
        extra = int.from_bytes(data[local + 28 : local + 30], "little")
        size = int.from_bytes(data[local + 18 : local + 22], "little")
        if bytes(data[local + 30 : local + 30 + length]) == name:
            start = local + 30 + length + extra + size // 3
            data[start : start + 24] = b"\xff" * 24
            return bytes(data)
        local = data.find(b"PK\x03\x04", local + 4)
    raise AssertionError(f"{member} not found")


def _protect(archive: bytes, *members: str) -> bytes:
    """Set the "encrypted" flag of the named entries (every entry when none
    is named) in both of their headers, as a password-protected ZIP has it."""
    data = bytearray(archive)
    for signature, flags, length_at, name_at in (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    ):
        header = data.find(signature)
        while header != -1:
            length = int.from_bytes(data[header + length_at : header + length_at + 2], "little")
            name = bytes(data[header + name_at : header + name_at + length]).decode()
            if not members or name in members:
                data[header + flags] |= 1
            header = data.find(signature, header + 4)
    return bytes(data)


def _varied_text(lines: int = 2000) -> bytes:
    """Text that stays a few kilobytes when compressed."""
    return "".join(
        f"line {index}: rate {index * 7919 % 1000}\n" for index in range(lines)
    ).encode()


def _documents(result: dict) -> dict[str, dict]:
    return {document["path"]: document for document in result["documents"]}


_RECREATE = "Re-create the archive (no password, standard compression) to include"


def test_a_corrupt_entry_leaves_the_rest_of_the_archive_readable():
    archive = _zip(
        {"a.md": b"first guide", "b.md": _varied_text(), "c.md": b"last guide"},
        method=zipfile.ZIP_DEFLATED,
    )
    workspace = _Workspace({"pack.zip": _corrupt_member(archive, "b.md")})

    result = sources.prepare_sources(workspace, ["pack.zip"])

    documents = _documents(result)
    assert documents["pack.zip!/a.md"]["content"] == "first guide"
    assert documents["pack.zip!/c.md"]["content"] == "last guide"
    # The damaged entry is named once and takes no document slot.
    assert list(documents) == ["pack.zip!/a.md", "pack.zip!/c.md"]
    assert result["warnings"] == [
        f"pack.zip: 1 entry was not read (corrupt): b.md. {_RECREATE} it."
    ]


def test_a_corrupt_lzma_entry_is_left_out_like_any_other():
    archive = _zip(
        {"a.md": b"first guide", "b.md": _varied_text(), "c.md": b"last guide"},
        method=zipfile.ZIP_LZMA,
    )
    workspace = _Workspace({"pack.zip": _corrupt_member(archive, "b.md")})

    result = sources.prepare_sources(workspace, ["pack.zip"])

    assert list(_documents(result)) == ["pack.zip!/a.md", "pack.zip!/c.md"]
    assert result["warnings"] == [
        f"pack.zip: 1 entry was not read (corrupt): b.md. {_RECREATE} it."
    ]


def test_an_unsupported_compression_method_leaves_out_only_that_entry():
    archive = _set_method(
        _zip({"a.md": b"alpha", "b.md": b"beta", "c.md": b"gamma", "d.md": b"delta"}),
        "b.md",
        99,
    )
    result = sources.prepare_sources(_Workspace({"pack.zip": archive}), ["pack.zip"])

    documents = _documents(result)
    assert {path: document["content"] for path, document in documents.items()} == {
        "pack.zip!/a.md": "alpha",
        "pack.zip!/c.md": "gamma",
        "pack.zip!/d.md": "delta",
    }
    (warning,) = result["warnings"]
    assert warning == (
        "pack.zip: 1 entry was not read (unsupported compression): b.md. "
        f"{_RECREATE} it."
    )


def test_a_password_protected_entry_leaves_out_only_that_entry():
    archive = _protect(
        _zip({"a.md": b"alpha", "prices.md": b"secret", "c.md": b"gamma"}), "prices.md"
    )
    result = sources.prepare_sources(_Workspace({"pack.zip": archive}), ["pack.zip"])

    assert list(_documents(result)) == ["pack.zip!/a.md", "pack.zip!/c.md"]
    assert "secret" not in json.dumps(result)
    (warning,) = result["warnings"]
    assert warning == (
        "pack.zip: 1 entry was not read (password-protected): prices.md. "
        f"{_RECREATE} it."
    )


def test_a_protected_archive_takes_one_slot_and_one_warning():
    # Each unreadable entry used to take a document slot and a warning: 100
    # of them filled the 60 slots and pushed the code file out.
    members = {f"doc-{index:03}.md": b"locked" for index in range(100)}
    workspace = _Workspace(
        {"source/locked.zip": _protect(_zip(members)), "source/zz/code.py": b"print('q')"}
    )
    result = sources.prepare_sources(workspace, ["source/"])

    documents = _documents(result)
    assert list(documents) == ["source/locked.zip", "source/zz/code.py"]
    assert documents["source/zz/code.py"]["content"] == "print('q')"
    assert documents["source/locked.zip"]["unreadable"] == (
        "100 entries could not be read (password-protected), so none of its "
        "files were read"
    )
    assert result["skipped_total"] == 0
    (warning,) = result["warnings"]
    assert warning == (
        "source/locked.zip: 100 entries could not be read (password-protected), "
        "so none of its files were read; studied by filename only."
    )


def test_unreadable_entries_take_no_document_slot(monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 2)
    members = {f"locked-{index}.md": b"x" for index in range(5)}
    archive = _protect(_zip({**members, "open.md": b"readable"}), *members)
    workspace = _Workspace({"pack.zip": archive, "zz.py": b"print('q')"})

    result = sources.prepare_sources(workspace, ["pack.zip", "zz.py"])

    assert list(_documents(result)) == ["pack.zip!/open.md", "zz.py"]
    assert result["skipped_total"] == 0


def test_mixed_entry_failures_are_counted_by_reason_and_named_to_fit():
    members = {f"notes/very-long-entry-name-{index:03}.md": b"x" for index in range(40)}
    archive = _zip({"open.md": b"readable", "bad.md": _varied_text(), **members},
                   method=zipfile.ZIP_DEFLATED)
    archive = _protect(_corrupt_member(archive, "bad.md"), *members)
    result = sources.prepare_sources(_Workspace({"pack.zip": archive}), ["pack.zip"])

    assert list(_documents(result)) == ["pack.zip!/open.md"]
    (warning,) = result["warnings"]
    assert warning.startswith(
        "pack.zip: 41 entries were not read (40 password-protected, 1 corrupt): "
        "notes/very-long-entry-name-000.md, "
    )
    assert " more. " in warning and warning.endswith(f"{_RECREATE} them.")
    assert len(warning) <= sources.SKIPPED_WARNING_CHARACTERS


@pytest.mark.parametrize(
    ("cap", "limit", "reason"),
    [
        ("MAX_ARCHIVE_FILES", 2, "archive has more than 2 files"),
        ("MAX_ARCHIVE_ENTRIES", 2, "archive has more than 2 entries"),
        ("MAX_ARCHIVE_BYTES", 1024 * 1024, "archive holds more than 1 MB of files"),
    ],
)
def test_an_archive_over_a_cap_says_which_cap(monkeypatch, cap, limit, reason):
    monkeypatch.setattr(sources, cap, limit)
    archive = _zip(
        {"a.md": b"a" * 600_000, "b.md": b"b" * 600_000, "c.md": b"c"},
        method=zipfile.ZIP_DEFLATED,
    )
    result = sources.prepare_sources(_Workspace({"big.zip": archive}), ["big.zip"])

    assert _documents(result) == {
        "big.zip": {
            "path": "big.zip",
            "content": "",
            "sha256": "",
            "truncated": False,
            "unreadable": f"{reason}, so none of its files were read",
        }
    }
    assert result["warnings"] == [
        f"big.zip: {reason}, so none of its files were read; studied by filename only."
    ]


def test_a_file_that_is_not_a_zip_is_named_as_such():
    result = sources.prepare_sources(
        _Workspace({"notes.zip": b"plain text, not an archive"}), ["notes.zip"]
    )
    assert _documents(result)["notes.zip"]["unreadable"].startswith(
        "not a readable ZIP archive"
    )


def test_a_parser_failure_marks_only_that_file_unreadable(monkeypatch):
    # HTMLParser raises AssertionError on some marked sections in the Python
    # the office image runs; it must cost that page only, not every source.
    def give_up(text: str) -> str:
        raise AssertionError("unknown status keyword 'foo' in marked section")

    monkeypatch.setattr(sources, "_html_text", give_up)
    result = sources.prepare_sources(
        _Workspace(
            {
                "source/a.md": b"first guide",
                "source/page.html": b"<p>x</p><![foo[x]]>",
                "source/z.md": b"last guide",
            }
        ),
        ["source/"],
    )

    documents = _documents(result)
    assert documents["source/a.md"]["content"] == "first guide"
    assert documents["source/z.md"]["content"] == "last guide"
    assert documents["source/page.html"]["content"] == ""
    assert documents["source/page.html"]["unreadable"] == sources.UNREADABLE_FORMAT


def test_markup_the_html_parser_rejects_never_drops_the_other_sources():
    # The real input: depending on the Python version the page is read or
    # reported unreadable, but the other sources are read either way.
    result = sources.prepare_sources(
        _Workspace(
            {
                "source/guide.md": b"the guide",
                "source/page.html": b"<p>before</p><![foo[x]]><p>after</p>",
            }
        ),
        ["source/"],
    )

    documents = _documents(result)
    assert documents["source/guide.md"]["content"] == "the guide"
    assert "source/page.html" in documents


def test_an_unexpected_failure_on_one_source_keeps_the_others(monkeypatch):
    class _Failing(_Workspace):
        def read_bytes(self, path: str, limit: int) -> bytes:
            if path == "source/b.md":
                raise RuntimeError("unexpected")
            return super().read_bytes(path, limit)

    result = sources.prepare_sources(
        _Failing(
            {"source/a.md": b"alpha", "source/b.md": b"beta", "source/c.md": b"gamma"}
        ),
        ["source/"],
    )

    documents = _documents(result)
    assert documents["source/a.md"]["content"] == "alpha"
    assert documents["source/c.md"]["content"] == "gamma"
    assert documents["source/b.md"]["unreadable"] == sources.READ_FAILED
    assert (
        f"source/b.md: {sources.READ_FAILED}; studied by filename only."
        in result["warnings"]
    )


def test_the_time_limit_stops_the_run_and_keeps_what_was_read():
    files = {f"source/{name}.md": name.encode() for name in ("a", "b", "c", "d")}
    workspace = _Workspace(files, time_limit_after=1)

    result = sources.prepare_sources(workspace, ["source/"])

    assert list(_documents(result)) == ["source/a.md"]
    assert workspace.reads == 2  # stopped at the first refusal, no retries
    first = result["warnings"][0]
    assert first.startswith(
        "3 selected sources were not read because the source reader's time "
        "limit was reached: source/b.md, source/c.md, source/d.md"
    )
    assert not any("missing, unsafe" in warning for warning in result["warnings"])


def test_a_listing_stopped_by_the_time_limit_names_what_was_already_listed():
    class _SlowListing(_Workspace):
        def list_files(self, path: str) -> list[str]:
            if path == "b/":
                raise TimeoutError("reader deadline")
            return super().list_files(path)

    workspace = _SlowListing(
        {"a/app.py": b"1", "a/guide.md": b"2", "b/x.md": b"3", "c/y.md": b"4"}
    )
    result = sources.prepare_sources(workspace, ["a/", "b/", "c/"])

    # Nothing can be read after the time limit: the files of a/ are named
    # (guides first) with the selections that were never listed.
    assert result["documents"] == [] and workspace.reads == 0
    (warning,) = result["warnings"]
    assert warning == (
        "4 selected sources were not read because the source reader's time "
        "limit was reached: a/guide.md, a/app.py, b/, c/. Select fewer or "
        "smaller sources and generate again."
    )


def test_an_archive_stopped_by_the_time_limit_is_named_partly(monkeypatch):
    archive = _zip({"a.md": b"alpha", "b.md": b"beta"})
    # Images sort after archives, so this one is still waiting to be read.
    workspace = _Workspace({"pack.zip": archive, "zz.png": b"\x89PNG"})
    real_open = zipfile.ZipFile.open
    calls = {"count": 0}

    def open_then_stop(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            raise TimeoutError("alarm")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", open_then_stop)
    result = sources.prepare_sources(workspace, ["pack.zip", "zz.png"])

    assert "pack.zip!/a.md" in _documents(result)
    assert "pack.zip (partly), zz.png" in result["warnings"][0]


def test_readable_code_takes_a_slot_before_an_image(monkeypatch):
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 1)
    workspace = _Workspace(
        {"source/logo.png": b"\x89PNG", "source/src/app.py": b"print('quote')"}
    )
    result = sources.prepare_sources(workspace, ["source/"])

    assert list(_documents(result)) == ["source/src/app.py"]
    assert result["skipped"] == ["source/logo.png"]


def test_a_text_file_without_an_extension_sorts_with_code(monkeypatch):
    # It is read as text, so it competes with code and wins on depth; it
    # does not wait behind deeper files with the images.
    monkeypatch.setattr(sources, "MAX_DOCUMENTS", 1)
    workspace = _Workspace(
        {"source/README": b"how we work", "source/src/app.py": b"print('quote')"}
    )
    result = sources.prepare_sources(workspace, ["source/"])

    assert list(_documents(result)) == ["source/README"]
    assert _documents(result)["source/README"]["content"] == "how we work"
    assert result["skipped"] == ["source/src/app.py"]


def test_a_build_file_name_does_not_make_an_office_file_text():
    # "Dockerfile.docx" matches the build-file names read as text; its bytes
    # are still an Office file, reported like any other.
    office_file = _zip({"word/document.xml": b"<w:document/>"}) + b"\xff\xfe\x00"
    result = sources.prepare_sources(
        _Workspace({"Dockerfile.docx": office_file, "Dockerfile.agent": b"FROM python"}),
        ["Dockerfile.docx", "Dockerfile.agent"],
    )

    documents = _documents(result)
    assert documents["Dockerfile.agent"]["content"] == "FROM python"
    assert documents["Dockerfile.docx"]["content"] == ""
    assert documents["Dockerfile.docx"]["unreadable"] == sources.UNREADABLE_FORMAT


class _Main:
    """Run the helper's entry point with the process limits and the protected
    workspace replaced, recording the alarm calls."""

    def __init__(self, monkeypatch, prepare, *, exit_error: Exception | None = None):
        self.alarms: list[int] = []
        self.workspace = types.SimpleNamespace(deadline=None)
        runner = self

        class _Context:
            def __enter__(self):
                return runner.workspace

            def __exit__(self, *exc):
                if exit_error is not None:
                    raise exit_error
                return False

        monkeypatch.setattr(sources, "resource", types.SimpleNamespace(
            setrlimit=lambda *args: None, RLIMIT_AS=0, RLIMIT_CPU=0
        ))
        monkeypatch.setattr(sources, "signal", types.SimpleNamespace(
            SIGALRM=0, signal=lambda *args: None, alarm=self.alarms.append
        ))
        monkeypatch.setattr(sources, "SecureWorkspace", _Context)
        monkeypatch.setattr(sources, "prepare_sources", prepare)
        monkeypatch.setattr(
            sources.sys, "stdin", types.SimpleNamespace(
                buffer=io.BytesIO(json.dumps({"paths": ["source/"]}).encode())
            )
        )

    def run(self, capsys) -> dict:
        assert sources.main() == 0
        return json.loads(capsys.readouterr().out)


_PREPARED = {"policy_version": sources.POLICY_VERSION, "documents": [], "warnings": ["kept"]}


def test_the_helper_prints_a_result_instead_of_a_traceback(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise KeyError("unexpected")

    main = _Main(monkeypatch, broken)
    assert main.run(capsys)["warnings"] == [
        "Source preparation failed; no source contents were studied."
    ]
    assert main.alarms == [sources.READER_SECONDS, 0]


def test_the_helper_reads_files_under_the_runs_one_time_limit(monkeypatch, capsys):
    seen = {}

    def prepare(workspace, paths, *, inventory_only):
        seen["deadline"] = workspace.deadline
        return _PREPARED

    main = _Main(monkeypatch, prepare)
    started = time.monotonic()
    assert main.run(capsys) == _PREPARED
    # File reads get the run's limit, not the 20 seconds of one Files
    # operation, and the alarm is off before the result is printed.
    assert 0 <= seen["deadline"] - started - sources.READER_SECONDS < 5
    assert main.alarms == [sources.READER_SECONDS, 0]
    assert sources.READER_SECONDS < 90  # the host's timeout for this helper


def test_an_alarm_after_the_sources_were_prepared_keeps_the_result(monkeypatch, capsys):
    main = _Main(monkeypatch, lambda *args, **kwargs: _PREPARED, exit_error=TimeoutError())
    assert main.run(capsys) == _PREPARED


@pytest.mark.parametrize("builder", ["skipped", "time"])
def test_named_warnings_fit_the_user_facing_cap(builder):
    names = [f"source/very/long/path/number-{index:03}.md" for index in range(40)]
    warning = (
        sources._skipped_warning(names, 526)
        if builder == "skipped"
        else sources._time_limit_warning(names, 40)
    )
    assert len(warning) <= sources.SKIPPED_WARNING_CHARACTERS
    assert warning.endswith("generate again.") or warning.endswith("the rest.")


def test_the_reader_and_the_daemon_share_one_warning_length():
    # The reader runs standalone in the office container and keeps a copy.
    assert sources.SKIPPED_WARNING_CHARACTERS == (
        _source_warnings.SOURCE_WARNING_CHARACTERS
    )


def test_only_an_unconverted_format_is_told_to_export():
    # The daemon keeps a copy of the reader's reason for a format it cannot
    # convert: the one unreadable case a text export fixes.
    assert _source_survey.UNREADABLE_FORMAT == sources.UNREADABLE_FORMAT
    assert _source_survey._unreadable_role(sources.UNREADABLE_FORMAT) == (
        "Present but unreadable; provide a text/CSV/HTML/PDF export."
    )
    # An archive over a cap, or one whose entries could not be read, says so.
    protected = (
        "100 entries could not be read (password-protected), so none of its "
        "files were read"
    )
    for reason in (
        "archive has more than 2,500 files, so none of its files were read",
        protected,
        "source byte limit exceeded",
    ):
        role = _source_survey._unreadable_role(reason)
        assert role == f"Present but unreadable: {reason}."
        assert "export" not in role
    # An odd value from an older or foreign reader keeps the general advice.
    assert "export" in _source_survey._unreadable_role(True)
