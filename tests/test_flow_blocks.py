"""Tests for the Flow Studio daemon block executor (FS-P2.T5 + T10).

Covers ``src/flow_blocks.py`` against a temp workspace with a fake
router + fake generation CLI: the ``ai`` kind (binding fill, schema
validation + one retry, ``--effort`` graceful degrade), the
``collect`` derive pass (materials-grounded happy path, the
structured-output retry, empty-derive ``ok: true``, enum-mismatch
drops, the workspace jail on material paths), ``generate``
(doc.yaml assembly, unresolved ``include_when`` skipping, resolved
include flags, artifacts),
``action`` (run_script / save_snapshot into the real datastore /
send_chat_notice / webhook_out / attach_artifacts — incl. the 256 KB
response-read cap), the ``(run_id, block_id)`` in-flight dedupe +
cached-result re-fire, the reconnect re-publish, and the
``flow_studio`` capability flag on the health report (FS-P2.T10
daemon half).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import src.flow_blocks as flow_blocks
from src.datastore import OfficeDatastore
from src.flow_blocks import FlowBlockExecutor, fill_bindings, fill_value


# ─── harness ───────────────────────────────────────────────────────────


class FakeRouter:
    def __init__(self, connected: bool = True) -> None:
        self.published: list[dict] = []
        self.ws_client = SimpleNamespace(connected=connected)

    async def publish_event(self, event: dict) -> None:
        self.published.append(event)


RUN = {
    "run_id": "run-1",
    "run_readable_id": "PR-001.F01",
    "workstream_id": "ws-1",
    "flow_id": "flow-1",
}

AI_SCHEMA = {
    "type": "object",
    "required": ["quote_total"],
    "properties": {"quote_total": {"type": "number"}},
}


def _make_executor(tmp_path, router=None, **kwargs) -> FlowBlockExecutor:
    return FlowBlockExecutor(
        router=router if router is not None else FakeRouter(),
        office_id="office-1",
        workspace_path=str(tmp_path),
        container_name="cbcl-office-test",
        **kwargs,
    )


def _cmd(kind: str, payload: dict, block_id: str = "b_x") -> dict:
    return {
        "type": "flow_block_execute",
        "run_id": "run-1",
        "block_id": block_id,
        "kind": kind,
        "payload": payload,
    }


def _ai_payload(**overrides) -> dict:
    payload = {
        "run": dict(RUN),
        "manifest": {"client": {"company": "Acme", "headcount": 12}},
        "item": None,
        "block_name": "Quote math",
        "goal": "compute the total",
        "prompt": "Compute the quote for {{manifest.client.company}}.",
        "inputs": [],
        "output_schema": dict(AI_SCHEMA),
        "effort": "medium",
    }
    payload.update(overrides)
    return payload


def _fake_cli(monkeypatch, responses: list):
    """Install a fake ``_run_claude_cli``; scripted responses, last one
    repeats. An Exception instance in the list is raised."""
    calls: list[dict] = []

    async def fake(container, system_prompt, user_prompt, timeout=0, effort=None, **kw):
        calls.append({"system": system_prompt, "user": user_prompt, "effort": effort})
        result = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(flow_blocks, "_run_claude_cli", fake)
    return calls


# ─── binding fill ──────────────────────────────────────────────────────


def test_fill_bindings_interpolates_and_blanks_missing():
    context = {"manifest": {"client": {"company": "Acme"}, "n": 3}}
    assert (
        fill_bindings("Hi {{manifest.client.company}} x{{manifest.n}}", context)
        == "Hi Acme x3"
    )
    assert fill_bindings("gone: {{manifest.nope}}!", context) == "gone: !"


def test_fill_value_preserves_types_for_sole_bindings():
    context = {"manifest": {"client": {"headcount": 12, "active": True}}}
    filled = fill_value(
        {
            "headcount": "{{manifest.client.headcount}}",
            "active": "{{manifest.client.active}}",
            "label": "hc={{manifest.client.headcount}}",
            "missing": "{{manifest.client.nope}}",
        },
        context,
    )
    assert filled["headcount"] == 12
    assert filled["active"] is True
    assert filled["label"] == "hc=12"
    assert filled["missing"] is None


# ─── ai blocks ─────────────────────────────────────────────────────────


async def test_ai_block_happy_path(tmp_path, monkeypatch):
    calls = _fake_cli(monkeypatch, [json.dumps({"quote_total": 12.5})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()

    assert len(router.published) == 1
    event = router.published[0]
    assert event["type"] == "flow_block_result"
    assert event["run_id"] == "run-1" and event["block_id"] == "b_x"
    assert event["ok"] is True
    assert event["output"] == {"quote_total": 12.5}
    # The prompt template was filled from the manifest snapshot.
    assert "Acme" in calls[0]["user"]
    assert "quote_total" in calls[0]["system"]


async def test_ai_block_schema_retry_recovers(tmp_path, monkeypatch):
    calls = _fake_cli(
        monkeypatch,
        [json.dumps({"wrong": 1}), json.dumps({"quote_total": 3})],
    )
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()

    assert len(calls) == 2
    assert "failed validation" in calls[1]["user"]
    assert router.published[0]["ok"] is True
    assert router.published[0]["output"] == {"quote_total": 3}


async def test_ai_block_fails_after_one_retry(tmp_path, monkeypatch):
    calls = _fake_cli(monkeypatch, [json.dumps({"wrong": 1})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()

    assert len(calls) == 2  # exactly ONE retry
    event = router.published[0]
    assert event["ok"] is False
    assert "schema validation" in event["error"]


async def test_ai_block_effort_degrade_on_unknown_flag(tmp_path, monkeypatch):
    calls = _fake_cli(
        monkeypatch,
        [
            RuntimeError("Claude CLI failed (rc=1): unknown option '--effort'"),
            json.dumps({"quote_total": 7}),
        ],
    )
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()

    assert router.published[0]["ok"] is True
    assert calls[1]["effort"] is None


# ─── collect blocks (the derive pass) ──────────────────────────────────


def _collect_fields() -> list[dict]:
    base = {
        "name": "",
        "type": "text",
        "options": [],
        "ref_to": "",
        "required": False,
        "derivable": False,
        "help": "",
    }
    return [
        dict(
            base,
            name="registered_country",
            derivable=True,
            required=True,
            help="ISO-2 country of registration",
        ),
        dict(base, name="headcount", type="number", derivable=True),
        dict(
            base,
            name="tier",
            type="select",
            options=["basic", "premium"],
            derivable=True,
        ),
        dict(base, name="client_name", required=True),  # ask-only
    ]


def _collect_payload(**overrides) -> dict:
    payload = {
        "run": dict(RUN),
        "manifest": {
            "client": {"company": "Acme"},
            "materials": ["materials/deal.md"],
        },
        "item": None,
        "block_name": "Intake — deal profile",
        "goal": "derive the deal profile",
        "card_title": "Deal profile",
        "fields": _collect_fields(),
        "derive_sources": ["materials", "chat"],
    }
    payload.update(overrides)
    return payload


def _write_material(tmp_path, rel="materials/deal.md", text=None) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        text
        if text is not None
        else "Deal memo\n\nRegistered country: DE\nHeadcount: 12\n"
    )


async def test_collect_derive_happy_path(tmp_path, monkeypatch):
    _write_material(tmp_path)
    calls = _fake_cli(
        monkeypatch,
        [
            json.dumps(
                {
                    "values": {"registered_country": "DE", "headcount": "12"},
                    "sources": {
                        "registered_country": "materials/deal.md",
                        "headcount": "materials/deal.md",
                    },
                }
            )
        ],
    )
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("collect", _collect_payload()))
    await executor.drain()

    assert len(router.published) == 1
    event = router.published[0]
    assert event["type"] == "flow_block_result"
    assert event["run_id"] == "run-1" and event["block_id"] == "b_x"
    assert event["ok"] is True
    # The richer {values, sources} shape, number coerced per field type.
    assert event["output"] == {
        "values": {"registered_country": "DE", "headcount": 12},
        "sources": {
            "registered_country": "materials/deal.md",
            "headcount": "materials/deal.md",
        },
    }
    # The session saw the material content, the field definitions with
    # their derivable/context markers, and the manifest snapshot.
    user = calls[0]["user"]
    assert "Registered country: DE" in user
    assert "registered_country" in user and "derivable" in user
    assert "client_name" in user and "do NOT fill" in user
    assert "Acme" in user
    # No-guessing + omit rules live in the system prompt.
    system = calls[0]["system"]
    assert "OMIT" in system and "guess" in system
    assert calls[0]["effort"] == "medium"


async def test_collect_derive_structured_output_retry(tmp_path, monkeypatch):
    _write_material(tmp_path)
    calls = _fake_cli(
        monkeypatch,
        [
            "The country is Germany, so I'd say DE.",  # not JSON → retry
            json.dumps(
                {
                    "values": {"registered_country": "DE"},
                    "sources": {"registered_country": "materials/deal.md"},
                }
            ),
        ],
    )
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("collect", _collect_payload()))
    await executor.drain()

    assert len(calls) == 2  # exactly ONE retry
    assert "failed validation" in calls[1]["user"]
    event = router.published[0]
    assert event["ok"] is True
    assert event["output"]["values"] == {"registered_country": "DE"}


async def test_collect_derive_fails_after_one_retry(tmp_path, monkeypatch):
    _write_material(tmp_path)
    calls = _fake_cli(monkeypatch, ["still not json"])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("collect", _collect_payload()))
    await executor.drain()

    assert len(calls) == 2
    event = router.published[0]
    assert event["ok"] is False
    assert "failed validation" in event["error"]


async def test_collect_empty_derive_is_ok(tmp_path, monkeypatch):
    _write_material(tmp_path, text="An unrelated note.\n")
    _fake_cli(monkeypatch, [json.dumps({"values": {}, "sources": {}})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("collect", _collect_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True  # empty derive = valid outcome, ask all
    assert event["output"] == {"values": {}, "sources": {}}


async def test_collect_derive_drops_misfits_keeps_grounded(tmp_path, monkeypatch):
    _write_material(tmp_path)
    _fake_cli(
        monkeypatch,
        [
            json.dumps(
                {
                    "values": {
                        "registered_country": "DE",  # kept
                        "tier": "platinum",  # not in options → dropped
                        "headcount": "around a dozen",  # not a number → dropped
                        "client_name": "Acme",  # not derivable → dropped
                        "invented": "x",  # undeclared → dropped
                    },
                    "sources": {
                        "registered_country": "materials/deal.md",
                        "tier": "materials/deal.md",
                        "headcount": "materials/deal.md",
                        "client_name": "materials/deal.md",
                    },
                }
            )
        ],
    )
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("collect", _collect_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True  # drops are never a block failure
    assert event["output"]["values"] == {"registered_country": "DE"}
    assert event["output"]["sources"] == {"registered_country": "materials/deal.md"}


async def test_collect_material_jail_refuses_traversal(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("HOST-SECRET-CONTENT")
    _write_material(workspace)
    calls = _fake_cli(monkeypatch, [json.dumps({"values": {}, "sources": {}})])
    router = FakeRouter()
    executor = _make_executor(workspace, router)
    payload = _collect_payload(
        manifest={
            "client": {"company": "Acme"},
            "materials": ["../secret.txt", "materials/deal.md"],
        }
    )
    await executor.handle_flow_block_execute(_cmd("collect", payload))
    await executor.drain()

    # The traversal path never reached the prompt; the in-jail
    # material did; the refusal is named, not fatal.
    user = calls[0]["user"]
    assert "HOST-SECRET-CONTENT" not in user
    assert "Registered country: DE" in user
    assert "not readable" in user and "../secret.txt" in user
    assert router.published[0]["ok"] is True


# ─── generate blocks ───────────────────────────────────────────────────


def _write_template(tmp_path) -> None:
    tdir = tmp_path / "templates" / "presale" / "quote"
    (tdir / "sections").mkdir(parents=True)
    (tdir / "doc.yaml").write_text(
        "title: Quote\n"
        "sections:\n"
        "  - file: sections/intro.md\n"
        "  - file: sections/dpa.md\n"
        '    include_when: "manifest.client.registered_country in EU"\n'
        "  - file: sections/summary.md\n"
        "    ai:\n"
        '      prompt: "Summarize for {{manifest.client.company}}"\n'
        "      max_words: 50\n"
    )
    (tdir / "sections" / "intro.md").write_text(
        "# Quote for {{manifest.client.company}}\n\n"
        "Total: {{manifest.quote.total}}\n"
    )
    (tdir / "sections" / "dpa.md").write_text("DPA ANNEX\n")
    (tdir / "sections" / "summary.md").write_text("## Summary\n")


def _generate_payload(**overrides) -> dict:
    payload = {
        "run": dict(RUN),
        "manifest": {
            "client": {"company": "Acme"},
            "quote": {"total": 100},
        },
        "item": None,
        "block_name": "Render pack",
        "goal": "render the quote",
        "documents": [
            {
                "template": "templates/presale/quote",
                "output": "outputs/{{manifest.client.company}}-quote.md",
            }
        ],
    }
    payload.update(overrides)
    return payload


async def test_generate_assembles_document_html_only(tmp_path, monkeypatch):
    _write_template(tmp_path)
    calls = _fake_cli(monkeypatch, ["A crisp summary."])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("generate", _generate_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    md_path = tmp_path / "outputs" / "PR" / "PR-001.F01" / "Acme-quote.md"
    assert md_path.is_file()
    text = md_path.read_text()
    assert "Quote for Acme" in text and "Total: 100" in text
    assert "A crisp summary." in text
    assert "DPA ANNEX" not in text  # unresolved include_when → skipped
    assert md_path.with_suffix(".html").is_file()
    doc = event["output"]["documents"][0]
    assert doc["unresolved_include_when"] == ["sections/dpa.md"]
    paths = [a["path"] for a in event["artifacts"]]
    assert "outputs/PR/PR-001.F01/Acme-quote.md" in paths
    assert "outputs/PR/PR-001.F01/Acme-quote.html" in paths
    assert not any(p.endswith(".pdf") for p in paths)
    # The ai-section ran through the generation CLI once.
    assert len(calls) == 1 and "Acme" in calls[0]["user"]


async def test_generate_honors_resolved_include_flags(tmp_path, monkeypatch):
    _write_template(tmp_path)
    _fake_cli(monkeypatch, ["Summary body."])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _generate_payload(include_flags={"sections/dpa.md": True})
    await executor.handle_flow_block_execute(_cmd("generate", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    doc = event["output"]["documents"][0]
    assert "unresolved_include_when" not in doc
    md_path = tmp_path / "outputs" / "PR" / "PR-001.F01" / "Acme-quote.md"
    assert "DPA ANNEX" in md_path.read_text()


async def test_generate_never_emits_a_pdf(tmp_path, monkeypatch):
    """07/FS-PDF-01: PDF output was removed (owner decision 2026-08-12).

    It never worked — weasyprint was not a declared dependency — and the
    `html_only` flag meant to admit that had no reader, so a flow whose
    deliverable was a PDF produced no file, no error and no explanation.
    Documents are Markdown + HTML; this pins that no .pdf artifact and no
    dead flag come back without a deliberate decision."""
    _write_template(tmp_path)
    _fake_cli(monkeypatch, ["Summary body."])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("generate", _generate_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    assert "html_only" not in event["output"]
    paths = [a["path"] for a in event["artifacts"]]
    assert not any(p.endswith(".pdf") for p in paths)
    assert not list((tmp_path / "outputs").rglob("*.pdf"))
    # The seams themselves are gone, so nothing can silently re-enable it.
    assert not hasattr(flow_blocks, "_pdf_available")
    assert not hasattr(flow_blocks, "_render_pdf_file")

#
# The workspace is bind-mounted rw into the office container (any agent
# can plant a symlink with Bash) while generate's reads/writes run
# HOST-side in the daemon process — an unresolved read/write would
# cross the container→host trust boundary (office-secrets store,
# ~/.cubicle/config.yaml). Lexical ../ checks cannot see a symlink.


def _jailed_workspace(tmp_path):
    """A workspace SUBDIR with a host-side secret sitting next to it —
    the layout a planted symlink would traverse into."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "host-secret.txt").write_text("HOST-SECRET-CONTENT")
    return workspace


async def test_generate_section_symlink_escape_refused(tmp_path, monkeypatch):
    workspace = _jailed_workspace(tmp_path)
    _write_template(workspace)
    tdir = workspace / "templates" / "presale" / "quote"
    (tdir / "sections" / "intro.md").unlink()
    # Relative link: 5 ups from sections/ lands next to the workspace —
    # exactly what a container-side `ln -s` can plant without knowing
    # the host layout.
    (tdir / "sections" / "intro.md").symlink_to(
        "../../../../../host-secret.txt"
    )
    assert (tdir / "sections" / "intro.md").resolve() == (
        tmp_path / "host-secret.txt"
    )
    _fake_cli(monkeypatch, ["Summary body."])
    router = FakeRouter()
    executor = _make_executor(workspace, router)
    await executor.handle_flow_block_execute(_cmd("generate", _generate_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is False
    assert "outside the office workspace" in event["error"]
    # The host file's content never landed in any output.
    out_dir = workspace / "outputs"
    dumped = [p.read_text() for p in out_dir.rglob("*") if p.is_file()]
    assert not any("HOST-SECRET-CONTENT" in text for text in dumped)


async def test_generate_doc_yaml_symlink_escape_refused(tmp_path, monkeypatch):
    workspace = _jailed_workspace(tmp_path)
    # A hostile doc.yaml symlink could exfiltrate any host-readable
    # file the yaml parser echoes back in its error, or drive reads.
    (tmp_path / "host-doc.yaml").write_text("title: Host\nsections: []\n")
    tdir = workspace / "templates" / "presale" / "quote"
    (tdir / "sections").mkdir(parents=True)
    (tdir / "doc.yaml").symlink_to("../../../../host-doc.yaml")
    _fake_cli(monkeypatch, ["irrelevant"])
    router = FakeRouter()
    executor = _make_executor(workspace, router)
    await executor.handle_flow_block_execute(_cmd("generate", _generate_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is False
    assert "outside the office workspace" in event["error"]


async def test_generate_output_symlink_escape_refused(tmp_path, monkeypatch):
    workspace = _jailed_workspace(tmp_path)
    _write_template(workspace)
    # Pre-planted outputs/ symlink: every write under it would land on
    # an arbitrary host path as the daemon's uid.
    host_target = tmp_path / "host-target"
    host_target.mkdir()
    (workspace / "outputs").symlink_to(host_target)
    _fake_cli(monkeypatch, ["Summary body."])
    router = FakeRouter()
    executor = _make_executor(workspace, router)
    await executor.handle_flow_block_execute(_cmd("generate", _generate_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is False
    assert "outside the office workspace" in event["error"]
    assert list(host_target.rglob("*")) == []  # nothing written through


async def test_generate_hostile_run_readable_id_sanitized_or_refused(
    tmp_path, monkeypatch
):
    """The outputs/ segments derive from a backend-supplied string —
    traversal characters are neutralized (the _document_output_name
    policy) and a segment that sanitizes to nothing is refused."""
    _write_template(tmp_path)
    _fake_cli(monkeypatch, ["Summary body."])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)

    # Separators/dot-runs are neutralized: "../../pwn" → "pwn", jailed.
    payload = _generate_payload(run=dict(RUN, run_readable_id="../../pwn"))
    await executor.handle_flow_block_execute(_cmd("generate", payload))
    await executor.drain()
    event = router.published[0]
    assert event["ok"] is True
    assert (tmp_path / "outputs" / "pwn" / "pwn").is_dir()
    assert not (tmp_path.parent / "pwn").exists()

    # A segment that sanitizes to NOTHING ("..") is refused outright.
    payload = _generate_payload(run=dict(RUN, run_readable_id=".."))
    await executor.handle_flow_block_execute(
        _cmd("generate", payload, block_id="b_dots")
    )
    await executor.drain()
    event = router.published[1]
    assert event["ok"] is False
    assert "safe output path" in event["error"]


# ─── ai inputs: file-vs-binding shadowing ─────────────────────────────


async def test_ai_input_file_named_like_binding_root_reads_the_file(
    tmp_path, monkeypatch
):
    """An input entry naming BOTH a workspace file and a binding root
    ('manifest.md') resolves as the FILE — the old binding-first
    classification silently rendered '- manifest.md: (no value)'."""
    (tmp_path / "manifest.md").write_text("SHIPPING MANIFEST CONTENT")
    calls = _fake_cli(monkeypatch, [json.dumps({"quote_total": 1})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _ai_payload(inputs=["manifest.md"])
    await executor.handle_flow_block_execute(_cmd("ai", payload))
    await executor.drain()

    user = calls[0]["user"]
    assert "SHIPPING MANIFEST CONTENT" in user
    assert "(no value)" not in user


# ─── activation identity + rework_note (gate-reject redo) ─────────────


async def test_new_activation_id_executes_fresh_despite_identical_payload(
    tmp_path, monkeypatch
):
    """A gate 'Request changes' redo re-activates the SAME block with a
    byte-identical payload — the backend-minted activation_id is what
    distinguishes it from a lost-result re-fire, so it must execute
    fresh instead of re-serving the rejected pass's cached result."""
    calls = _fake_cli(monkeypatch, [json.dumps({"quote_total": 1})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    first = dict(_cmd("ai", _ai_payload()), activation_id="act-1")
    redo = dict(_cmd("ai", _ai_payload()), activation_id="act-2")

    await executor.handle_flow_block_execute(first)
    await executor.drain()
    await executor.handle_flow_block_execute(redo)
    await executor.drain()
    assert len(calls) == 2  # identical payload, new activation → fresh
    assert len(router.published) == 2

    # The SAME activation re-fired (lost result) still serves cache.
    await executor.handle_flow_block_execute(dict(redo))
    await executor.drain()
    assert len(calls) == 2
    assert len(router.published) == 3


async def test_rework_note_reaches_ai_and_generate_prompts(tmp_path, monkeypatch):
    """A threaded ``payload.rework_note`` must actually reach the
    generation prompts (and, as a side effect, change the payload hash)
    — otherwise a gate-reject redo re-produces the rejected output."""
    calls = _fake_cli(monkeypatch, [json.dumps({"quote_total": 2})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _ai_payload(rework_note="The total is missing the discount.")
    await executor.handle_flow_block_execute(_cmd("ai", payload))
    await executor.drain()
    assert "Rework feedback" in calls[0]["user"]
    assert "missing the discount" in calls[0]["user"]

    # generate: the ai-section prompt carries the note too.
    _write_template(tmp_path)
    gen_calls = _fake_cli(monkeypatch, ["Section body."])
    gen_payload = _generate_payload(rework_note="Drop the DPA mention.")
    await executor.handle_flow_block_execute(
        _cmd("generate", gen_payload, block_id="b_gen")
    )
    await executor.drain()
    assert "Drop the DPA mention." in gen_calls[0]["user"]


# ─── cost telemetry (spec §11) ────────────────────────────────────────


def _fake_cli_with_cost(monkeypatch, responses: list, cost_each: float):
    """A fake CLI that reports a per-call cost through ``cost_sink`` —
    the ``--output-format json`` envelope path."""
    calls: list[dict] = []

    async def fake(
        container, system_prompt, user_prompt,
        timeout=0, effort=None, cost_sink=None, **kw,
    ):
        calls.append({"user": user_prompt})
        if cost_sink is not None:
            cost_sink.append(cost_each)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(flow_blocks, "_run_claude_cli", fake)
    return calls


async def test_ai_block_result_carries_cost_usd(tmp_path, monkeypatch):
    _fake_cli_with_cost(monkeypatch, [json.dumps({"quote_total": 1})], 0.031)
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    assert event["cost_usd"] == 0.031


async def test_generate_accumulates_cost_across_ai_sections(tmp_path, monkeypatch):
    _write_template(tmp_path)
    _fake_cli_with_cost(monkeypatch, ["Summary body."], 0.02)
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("generate", _generate_payload()))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    assert event["cost_usd"] == 0.02  # one ai-section in the template


async def test_costless_result_omits_the_field(tmp_path, monkeypatch):
    """Older CLI envelopes / zero-capture runs omit cost_usd — the
    backend's ``or 0.0`` read handles absence; a fabricated 0 would be
    indistinguishable from a genuine free run."""
    _fake_cli(monkeypatch, [json.dumps({"quote_total": 1})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()
    assert "cost_usd" not in router.published[0]


# ─── action blocks ─────────────────────────────────────────────────────


def _clients_config_store():
    field = {
        "name": "company",
        "type": "text",
        "options": [],
        "ref_to": "",
        "required": True,
        "help": "",
    }
    headcount = dict(field, name="headcount", type="number", required=False)
    return SimpleNamespace(
        collections=[
            {
                "name": "clients",
                "display_name": "Clients",
                "schema": [field, headcount],
                "schema_revision": 1,
            }
        ]
    )


def _action_payload(kind: str, params: dict, collection: str = "") -> dict:
    return {
        "run": dict(RUN),
        "manifest": {"client": {"company": "Acme", "headcount": 12}},
        "item": None,
        "block_name": "Do the thing",
        "goal": "",
        "kind": kind,
        "collection": collection,
        "params": params,
    }


async def test_action_save_snapshot_upserts_into_datastore(tmp_path):
    store = OfficeDatastore(tmp_path / "office.sqlite", _clients_config_store())
    router = FakeRouter()
    executor = _make_executor(tmp_path, router, datastore=store)
    payload = _action_payload(
        "save_snapshot",
        {
            "data": {
                "company": "{{manifest.client.company}}",
                "headcount": "{{manifest.client.headcount}}",
            }
        },
        collection="clients",
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    row_id = event["output"]["row_id"]
    row = await store.get_row("clients", row_id)
    assert row["row"]["data"] == {"company": "Acme", "headcount": 12}


async def test_action_save_snapshot_schema_error_is_honest(tmp_path):
    store = OfficeDatastore(tmp_path / "office.sqlite", _clients_config_store())
    router = FakeRouter()
    executor = _make_executor(tmp_path, router, datastore=store)
    payload = _action_payload(
        "save_snapshot",
        {"data": {"nope": 1}},
        collection="clients",
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is False
    assert "unknown field" in event["error"]


async def test_action_run_script_attributes_the_flow(tmp_path):
    runner = SimpleNamespace(execute=AsyncMock(return_value="exec-123"))
    router = FakeRouter()
    executor = _make_executor(tmp_path, router, script_runner=runner)
    payload = _action_payload("run_script", {"script_name": "sync-crm"})
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    assert event["output"] == {"execution_id": "exec-123"}
    kwargs = runner.execute.await_args.kwargs
    assert kwargs["script_name"] == "sync-crm"
    assert kwargs["triggered_by"] == "flow:PR-001.F01"
    assert kwargs["workstream_short_code"] == "PR"


async def test_action_send_chat_notice(tmp_path, monkeypatch):
    fake_post = AsyncMock(return_value=True)
    monkeypatch.setattr(flow_blocks, "post_system_chat_notice", fake_post)
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _action_payload(
        "send_chat_notice",
        {"message": "Pack ready for {{manifest.client.company}}"},
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    assert router.published[0]["ok"] is True
    args = fake_post.await_args
    assert args.args[2] == "workstream:ws-1"
    assert "Acme" in args.args[3]
    assert args.kwargs["action_payload"]["kind"] == "flow_event"
    assert args.kwargs["action_payload"]["flow_run_id"] == "run-1"


async def test_action_webhook_out_posts_filled_body(tmp_path, monkeypatch):
    fake_request = AsyncMock(return_value=(200, "ok"))
    monkeypatch.setattr(flow_blocks, "_webhook_request", fake_request)
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _action_payload(
        "webhook_out",
        {
            "url": "https://example.com/hook",
            "payload": {"company": "{{manifest.client.company}}"},
        },
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    assert router.published[0]["ok"] is True
    method, url, headers, body = fake_request.await_args.args
    assert method == "POST" and url == "https://example.com/hook"
    assert json.loads(body) == {"company": "Acme"}


async def test_action_webhook_out_http_error_fails_block(tmp_path, monkeypatch):
    monkeypatch.setattr(
        flow_blocks, "_webhook_request", AsyncMock(return_value=(503, "down"))
    )
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _action_payload(
        "webhook_out", {"url": "https://example.com/hook", "payload": {}}
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is False
    assert "HTTP 503" in event["error"]


async def test_webhook_response_read_capped_at_256kb(monkeypatch):
    """O7: the real ``_webhook_request`` stops READING the response
    body at 256 KB — an endless body never balloons daemon memory; the
    2000-char preview is sliced from the capped buffer."""
    import httpx

    chunk = b"x" * 65_536  # 64 KB per chunk

    class EndlessStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks_yielded = 0

        async def __aiter__(self):
            for _ in range(1000):  # would be ~64 MB if fully drained
                self.chunks_yielded += 1
                yield chunk

    stream = EndlessStream()
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    status, preview = await flow_blocks._webhook_request(
        "POST", "https://example.com/hook", {}, b"{}"
    )
    assert status == 200
    assert preview == "x" * flow_blocks._WEBHOOK_PREVIEW_CHARS
    # 256 KB / 64 KB = 4 chunks; a small read-ahead is tolerated, a
    # drained stream is not.
    assert stream.chunks_yielded <= 6


async def test_action_attach_artifacts(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "x.md").write_text("hello")
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _action_payload(
        "attach_artifacts",
        {
            "artifacts": [
                {"path": "outputs/x.md", "label": "X"},
                {"path": "outputs/missing.md", "label": "M"},
            ]
        },
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    assert event["artifacts"] == [{"path": "outputs/x.md", "label": "X"}]
    assert event["output"]["missing"] == ["outputs/missing.md"]


async def test_action_attach_artifacts_accepts_plain_string_entries(tmp_path):
    """The step editor's Artifacts field emits plain path strings
    (comma-separated); the daemon must accept them alongside the
    ``{path, label}`` dict shape — pre-fix every string entry was
    silently skipped (the ``isinstance(entry, dict)`` filter) and the
    action always failed with an EMPTY "found none of the named
    files:" error."""
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "x.md").write_text("hello")
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    payload = _action_payload(
        "attach_artifacts",
        {"artifacts": ["outputs/x.md", "outputs/missing.md"]},
    )
    await executor.handle_flow_block_execute(_cmd("action", payload))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is True
    assert event["artifacts"] == [
        {"path": "outputs/x.md", "label": "outputs/x.md"}
    ]
    assert event["output"]["missing"] == ["outputs/missing.md"]


async def test_unknown_kind_reports_upgrade(tmp_path):
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("teleport", {"run": dict(RUN)}))
    await executor.drain()

    event = router.published[0]
    assert event["ok"] is False
    assert "upgrade cbcl" in event["error"]


async def test_malformed_command_is_dropped(tmp_path):
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute({"type": "flow_block_execute"})
    await executor.drain()
    assert router.published == []


# ─── dedupe + re-fire ─────────────────────────────────────────────────


async def test_inflight_dedupe_and_cached_result_refire(tmp_path, monkeypatch):
    gate = asyncio.Event()
    calls: list[int] = []

    async def slow_cli(container, system_prompt, user_prompt, **kw):
        calls.append(1)
        await gate.wait()
        return json.dumps({"quote_total": 1})

    monkeypatch.setattr(flow_blocks, "_run_claude_cli", slow_cli)
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    command = _cmd("ai", _ai_payload())

    await executor.handle_flow_block_execute(command)
    # Backend re-fire while the first execution is in flight → dropped.
    await executor.handle_flow_block_execute(command)
    gate.set()
    await executor.drain()
    assert len(calls) == 1
    assert len(router.published) == 1

    # A re-fire AFTER completion re-sends the cached result without
    # re-executing (the result likely got lost in transit).
    await executor.handle_flow_block_execute(command)
    await executor.drain()
    assert len(calls) == 1
    assert len(router.published) == 2
    assert router.published[0] == router.published[1]


async def test_changed_payload_executes_fresh(tmp_path, monkeypatch):
    calls = _fake_cli(monkeypatch, [json.dumps({"quote_total": 1})])
    router = FakeRouter()
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()
    # A loop frame / gate reroute re-activates the SAME block with a
    # different payload — that must execute fresh, never serve cache.
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload(item={"index": 1})))
    await executor.drain()
    assert len(calls) == 2
    assert len(router.published) == 2


async def test_reconnect_republishes_undelivered_results(tmp_path, monkeypatch):
    _fake_cli(monkeypatch, [json.dumps({"quote_total": 1})])
    router = FakeRouter(connected=False)
    executor = _make_executor(tmp_path, router)
    await executor.handle_flow_block_execute(_cmd("ai", _ai_payload()))
    await executor.drain()
    assert len(router.published) == 1  # went to the (lossy) replay queue

    router.ws_client.connected = True
    await executor.on_reconnect({})
    assert len(router.published) == 2
    assert router.published[1]["type"] == "flow_block_result"

    # Once delivered while connected, a further reconnect is a no-op.
    await executor.on_reconnect({})
    assert len(router.published) == 2


# ─── capability flag (FS-P2.T10 daemon half) ──────────────────────────


async def test_flow_studio_capability_on_health_report():
    from src.health.reporter import DAEMON_CAPABILITIES, HealthReporter

    assert "flow_studio" in DAEMON_CAPABILITIES
    reporter = HealthReporter(office_id="office-1")
    report = await reporter._build_report()
    assert report["type"] == "health_report"
    assert "flow_studio" in report["capabilities"]
