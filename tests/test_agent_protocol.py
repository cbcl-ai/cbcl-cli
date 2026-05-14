"""Tests for agent_protocol.py -- IPC message types and serialization."""

import asyncio
import io
import json
from dataclasses import asdict

import pytest

from src.agent_protocol import (
    AGENT_TO_ORCHESTRATOR_TYPES,
    MESSAGE_CLASSES,
    ORCHESTRATOR_TO_AGENT_TYPES,
    AssignTaskMessage,
    CancelTaskMessage,
    ChatMessage,
    ErrorMessage,
    MessageType,
    PingMessage,
    PongMessage,
    ProgressMessage,
    ReadyMessage,
    ResponseChunkMessage,
    ResponseFinalMessage,
    ShutdownMessage,
    TaskCompleteMessage,
    ToolCallMessage,
    ToolResponseMessage,
    deserialize,
    read_messages,
    serialize,
    write_message,
)


# ---------------------------------------------------------------------------
# MessageType enum tests
# ---------------------------------------------------------------------------


class TestMessageType:
    """Tests for the MessageType enum."""

    def test_all_types_exist(self) -> None:
        """Verify the enum size matches the two direction sets."""
        # ACTIVITY was added alongside token-level Manager streaming —
        # see agent_protocol.py / manager_controller._on_activity. Keep
        # this assertion in terms of the sets so future additions don't
        # require coordinated edits in three places.
        assert len(MessageType) == (
            len(ORCHESTRATOR_TO_AGENT_TYPES) + len(AGENT_TO_ORCHESTRATOR_TYPES)
        )

    def test_orchestrator_to_agent_count(self) -> None:
        """There are 6 Orchestrator -> Agent message types."""
        assert len(ORCHESTRATOR_TO_AGENT_TYPES) == 6

    def test_agent_to_orchestrator_count(self) -> None:
        """Agent -> Orchestrator: READY, PROGRESS, TOOL_CALL,
        TASK_COMPLETE, RESPONSE_CHUNK, RESPONSE_FINAL, ACTIVITY,
        ERROR, PONG = 9."""
        assert len(AGENT_TO_ORCHESTRATOR_TYPES) == 9

    def test_no_overlap(self) -> None:
        """Orchestrator and agent message sets do not overlap."""
        overlap = ORCHESTRATOR_TO_AGENT_TYPES & AGENT_TO_ORCHESTRATOR_TYPES
        assert len(overlap) == 0, f"Overlapping types: {overlap}"

    def test_complete_coverage(self) -> None:
        """All enum members are in exactly one direction set."""
        all_types = ORCHESTRATOR_TO_AGENT_TYPES | AGENT_TO_ORCHESTRATOR_TYPES
        enum_values = {member.value for member in MessageType}
        # Convert frozenset of enum members to string values for comparison
        all_type_values = {t.value if isinstance(t, MessageType) else t for t in all_types}
        assert all_type_values == enum_values

    def test_enum_values_are_strings(self) -> None:
        """All enum values are lowercase strings."""
        for member in MessageType:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()

    def test_message_classes_registry_complete(self) -> None:
        """MESSAGE_CLASSES has an entry for every MessageType."""
        for member in MessageType:
            assert member in MESSAGE_CLASSES or member.value in MESSAGE_CLASSES


# ---------------------------------------------------------------------------
# Orchestrator -> Agent message tests
# ---------------------------------------------------------------------------


class TestAssignTaskMessage:
    """Tests for the AssignTaskMessage dataclass."""

    def test_default_type(self) -> None:
        msg = AssignTaskMessage()
        assert msg.type == MessageType.ASSIGN_TASK

    def test_type_not_overridable(self) -> None:
        """Type field is init=False, cannot be passed in constructor."""
        msg = AssignTaskMessage(task_id="t1")
        assert msg.type == MessageType.ASSIGN_TASK

    def test_all_fields_present(self) -> None:
        msg = AssignTaskMessage(
            task_id="uuid-123",
            readable_id="WR-001.T01",
            title="Test task",
            brief={"goal": "Do something"},
            agent_config={"name": "analyst", "model": "claude-sonnet-4-6"},
            workspace_path="/workspace",
            session_id="session-abc",
            rework_feedback="Fix the tests",
            rework_count=1,
            workstream_name="Website Redesign",
            backend_url="http://localhost:8000",
            office_id="office-uuid",
        )
        data = asdict(msg)
        assert data["type"] == "assign_task"
        assert data["task_id"] == "uuid-123"
        assert data["readable_id"] == "WR-001.T01"
        assert data["brief"]["goal"] == "Do something"
        assert data["rework_feedback"] == "Fix the tests"
        assert data["rework_count"] == 1

    def test_default_values(self) -> None:
        msg = AssignTaskMessage()
        assert msg.task_id == ""
        assert msg.brief == {}
        assert msg.session_id is None
        assert msg.rework_feedback is None
        assert msg.rework_count == 0


class TestChatMessage:
    """Tests for the ChatMessage dataclass."""

    def test_default_type(self) -> None:
        msg = ChatMessage()
        assert msg.type == MessageType.CHAT_MESSAGE

    def test_full_construction(self) -> None:
        msg = ChatMessage(
            context_key="workstream:uuid-ws",
            content="I need help",
            context_data={"workstream_name": "Recruitment"},
            conversation_id="conv-123",
            session_id="session-abc",
        )
        data = asdict(msg)
        assert data["context_key"] == "workstream:uuid-ws"
        assert data["content"] == "I need help"
        assert data["conversation_id"] == "conv-123"


class TestToolResponseMessage:
    """Tests for the ToolResponseMessage dataclass."""

    def test_success_response(self) -> None:
        msg = ToolResponseMessage(
            request_id="req-001",
            result={"task_id": "uuid-new", "readable_id": "WR-001.T02"},
        )
        data = asdict(msg)
        assert data["request_id"] == "req-001"
        assert data["result"]["task_id"] == "uuid-new"
        assert data["error"] is None

    def test_error_response(self) -> None:
        msg = ToolResponseMessage(
            request_id="req-002",
            error="Task not found",
        )
        data = asdict(msg)
        assert data["result"] is None
        assert data["error"] == "Task not found"


class TestCancelTaskMessage:
    """Tests for the CancelTaskMessage dataclass."""

    def test_default_type(self) -> None:
        msg = CancelTaskMessage(reason="Manager cancelled the task")
        assert msg.type == MessageType.CANCEL_TASK
        assert msg.reason == "Manager cancelled the task"


class TestShutdownMessage:
    """Tests for the ShutdownMessage dataclass."""

    def test_default_grace_period(self) -> None:
        msg = ShutdownMessage()
        assert msg.grace_period_seconds == 30

    def test_custom_grace_period(self) -> None:
        msg = ShutdownMessage(grace_period_seconds=60)
        assert msg.grace_period_seconds == 60


class TestPingMessage:
    """Tests for the PingMessage dataclass."""

    def test_default_type(self) -> None:
        msg = PingMessage()
        assert msg.type == MessageType.PING
        data = asdict(msg)
        assert data == {"type": "ping"}


# ---------------------------------------------------------------------------
# Agent -> Orchestrator message tests
# ---------------------------------------------------------------------------


class TestReadyMessage:
    """Tests for the ReadyMessage dataclass."""

    def test_full_construction(self) -> None:
        msg = ReadyMessage(pid=12345, agent_name="analyst")
        data = asdict(msg)
        assert data["type"] == "ready"
        assert data["pid"] == 12345
        assert data["agent_name"] == "analyst"


class TestProgressMessage:
    """Tests for the ProgressMessage dataclass."""

    def test_checkpoint_event(self) -> None:
        msg = ProgressMessage(
            task_id="uuid-task",
            event_type="checkpoint",
            content="Reading Nav.tsx to understand structure...",
            details={"files_read": ["src/components/Nav.tsx"]},
            token_cost=0.03,
        )
        data = asdict(msg)
        assert data["event_type"] == "checkpoint"
        assert data["content"] == "Reading Nav.tsx to understand structure..."
        assert data["token_cost"] == 0.03

    def test_tool_run_event(self) -> None:
        msg = ProgressMessage(
            task_id="uuid-task",
            event_type="tool_run",
            content="Using Bash",
            details={"tool": "Bash"},
        )
        data = asdict(msg)
        assert data["event_type"] == "tool_run"

    def test_optional_fields(self) -> None:
        msg = ProgressMessage(task_id="t1", event_type="checkpoint", content="hi")
        data = asdict(msg)
        assert data["details"] is None
        assert data["token_cost"] is None


class TestToolCallMessage:
    """Tests for the ToolCallMessage dataclass."""

    def test_create_task_tool_call(self) -> None:
        msg = ToolCallMessage(
            request_id="req-abc-123",
            tool="kanban.create_task",
            params={
                "workstream_id": "uuid-ws",
                "title": "Research Python devs",
                "priority": "high",
            },
        )
        data = asdict(msg)
        assert data["request_id"] == "req-abc-123"
        assert data["tool"] == "kanban.create_task"
        assert data["params"]["priority"] == "high"

    def test_request_id_correlation(self) -> None:
        """ToolCall and ToolResponse share the same request_id."""
        call = ToolCallMessage(request_id="req-001", tool="kanban.get_board")
        response = ToolResponseMessage(
            request_id="req-001",
            result={"tasks": []},
        )
        assert asdict(call)["request_id"] == asdict(response)["request_id"]


class TestTaskCompleteMessage:
    """Tests for the TaskCompleteMessage dataclass."""

    def test_review_status(self) -> None:
        msg = TaskCompleteMessage(
            task_id="uuid-task",
            status="review",
            comment="Implementation complete.",
            token_cost=0.42,
            session_id="session-xyz",
        )
        data = asdict(msg)
        assert data["status"] == "review"
        assert data["token_cost"] == 0.42

    def test_blocked_status(self) -> None:
        msg = TaskCompleteMessage(task_id="t1", status="blocked", comment="Cancelled.")
        assert msg.status == "blocked"

    def test_default_status_is_review(self) -> None:
        msg = TaskCompleteMessage()
        assert msg.status == "review"


class TestResponseChunkMessage:
    """Tests for the ResponseChunkMessage dataclass."""

    def test_streaming_chunk(self) -> None:
        msg = ResponseChunkMessage(
            conversation_id="conv-123",
            context_key="workstream:uuid-ws",
            content="I'll help you with that. Let me ",
        )
        data = asdict(msg)
        assert data["type"] == "response_chunk"
        assert data["content"] == "I'll help you with that. Let me "


class TestResponseFinalMessage:
    """Tests for the ResponseFinalMessage dataclass."""

    def test_final_message(self) -> None:
        msg = ResponseFinalMessage(
            conversation_id="conv-123",
            context_key="general_chat",
            token_cost=0.08,
            session_id="session-gc-001",
        )
        data = asdict(msg)
        assert data["type"] == "response_final"
        assert data["token_cost"] == 0.08


class TestErrorMessage:
    """Tests for the ErrorMessage dataclass."""

    def test_non_fatal_error(self) -> None:
        msg = ErrorMessage(
            message="Tool call failed: rate limited",
            task_id="uuid-task",
            fatal=False,
        )
        data = asdict(msg)
        assert data["fatal"] is False

    def test_fatal_error(self) -> None:
        msg = ErrorMessage(message="SDK crash", fatal=True)
        assert msg.fatal is True
        assert msg.task_id is None


class TestPongMessage:
    """Tests for the PongMessage dataclass."""

    def test_default_type(self) -> None:
        msg = PongMessage()
        data = asdict(msg)
        assert data == {"type": "pong"}


# ---------------------------------------------------------------------------
# Serialization helper tests
# ---------------------------------------------------------------------------


class TestSerialize:
    """Tests for the serialize() function."""

    def test_dataclass_serialize(self) -> None:
        msg = PingMessage()
        result = serialize(msg)
        assert result == '{"type":"ping"}'

    def test_dict_serialize(self) -> None:
        result = serialize({"type": "ping"})
        assert result == '{"type":"ping"}'

    def test_compact_separators(self) -> None:
        """No spaces in output (compact JSON)."""
        msg = ReadyMessage(pid=1, agent_name="test")
        result = serialize(msg)
        assert " " not in result

    def test_single_line(self) -> None:
        """Output is a single line (no embedded newlines)."""
        msg = ProgressMessage(
            task_id="t1",
            event_type="checkpoint",
            content="Line 1\nLine 2\nLine 3",
        )
        result = serialize(msg)
        # json.dumps escapes newlines as \\n, so the output is one line
        assert "\n" not in result

    def test_complex_nested_dict(self) -> None:
        msg = AssignTaskMessage(
            task_id="uuid-123",
            brief={
                "goal": "Implement responsive navigation",
                "acceptance_criteria": ["Works on 320px", "Hamburger menu"],
                "allowed_tools": ["Read", "Write", "Bash"],
            },
            agent_config={
                "name": "python-developer",
                "model": "claude-sonnet-4-6",
                "allowed_tools": ["Read", "Write", "Bash"],
            },
        )
        result = serialize(msg)
        parsed = json.loads(result)
        assert parsed["brief"]["acceptance_criteria"] == ["Works on 320px", "Hamburger menu"]

    def test_non_json_native_types(self) -> None:
        """default=str handles UUIDs, enums, etc."""
        from enum import Enum

        class Status(Enum):
            ACTIVE = "active"

        msg = {"type": "test", "status": Status.ACTIVE}
        result = serialize(msg)
        parsed = json.loads(result)
        assert parsed["status"] == "Status.ACTIVE"

    def test_invalid_type_raises(self) -> None:
        with pytest.raises(TypeError, match="Cannot serialize"):
            serialize(42)

    def test_none_values_preserved(self) -> None:
        msg = ToolResponseMessage(request_id="r1", result=None, error=None)
        result = serialize(msg)
        parsed = json.loads(result)
        assert parsed["result"] is None
        assert parsed["error"] is None


class TestDeserialize:
    """Tests for the deserialize() function."""

    def test_basic_parse(self) -> None:
        result = deserialize('{"type":"ping"}')
        assert result == {"type": "ping"}

    def test_strips_whitespace(self) -> None:
        result = deserialize('  {"type":"pong"}  \n')
        assert result == {"type": "pong"}

    def test_round_trip(self) -> None:
        """serialize -> deserialize produces equivalent dict."""
        msg = AssignTaskMessage(
            task_id="uuid-task",
            readable_id="WR-001.T01",
            title="Test task",
            brief={"goal": "Test"},
        )
        line = serialize(msg)
        parsed = deserialize(line)
        original = asdict(msg)
        assert parsed == original

    def test_round_trip_all_message_types(self) -> None:
        """Every message type survives a serialize -> deserialize round trip."""
        messages = [
            AssignTaskMessage(task_id="t1", readable_id="WR-001.T01"),
            ChatMessage(context_key="general_chat", content="hello"),
            ToolResponseMessage(request_id="r1", result={"ok": True}),
            CancelTaskMessage(reason="cancelled"),
            ShutdownMessage(grace_period_seconds=10),
            PingMessage(),
            ReadyMessage(pid=100, agent_name="analyst"),
            ProgressMessage(task_id="t1", event_type="checkpoint", content="done"),
            ToolCallMessage(request_id="r1", tool="kanban.get_board", params={}),
            TaskCompleteMessage(task_id="t1", token_cost=0.5, session_id="s1"),
            ResponseChunkMessage(conversation_id="c1", context_key="gc", content="hi"),
            ResponseFinalMessage(conversation_id="c1", context_key="gc", token_cost=0.1),
            ErrorMessage(message="oops", fatal=False),
            PongMessage(),
        ]
        for msg in messages:
            line = serialize(msg)
            parsed = deserialize(line)
            original = asdict(msg)
            assert parsed == original, f"Round trip failed for {type(msg).__name__}"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            deserialize("not json")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            deserialize("")


class TestWriteMessage:
    """Tests for the write_message() function."""

    def test_writes_to_stream(self) -> None:
        stream = io.StringIO()
        write_message(PingMessage(), stream=stream)
        output = stream.getvalue()
        assert output == '{"type":"ping"}\n'

    def test_flushes_after_write(self) -> None:
        """Verify flush is called (important for pipe communication)."""

        class FlushTracker(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flush_count = 0

            def flush(self):
                self.flush_count += 1
                super().flush()

        stream = FlushTracker()
        write_message(PongMessage(), stream=stream)
        assert stream.flush_count >= 1

    def test_newline_terminated(self) -> None:
        stream = io.StringIO()
        write_message(ReadyMessage(pid=1, agent_name="test"), stream=stream)
        output = stream.getvalue()
        assert output.endswith("\n")

    def test_single_line_output(self) -> None:
        """Output is exactly one line (message + newline)."""
        stream = io.StringIO()
        write_message(
            AssignTaskMessage(task_id="t1", title="Multi\nLine\nTitle"),
            stream=stream,
        )
        output = stream.getvalue()
        lines = output.strip().split("\n")
        assert len(lines) == 1

    def test_dict_input(self) -> None:
        stream = io.StringIO()
        write_message({"type": "custom", "data": 42}, stream=stream)
        output = stream.getvalue()
        parsed = json.loads(output.strip())
        assert parsed["type"] == "custom"
        assert parsed["data"] == 42


class TestReadMessages:
    """Tests for the read_messages() async generator."""

    @pytest.fixture
    def make_stream(self):
        """Create an asyncio StreamReader with predefined data."""

        def _make(lines: list[str]) -> asyncio.StreamReader:
            reader = asyncio.StreamReader()
            data = "".join(line + "\n" for line in lines).encode()
            reader.feed_data(data)
            reader.feed_eof()
            return reader

        return _make

    @pytest.mark.asyncio
    async def test_reads_single_message(self, make_stream) -> None:
        stream = make_stream(['{"type":"ready","pid":1,"agent_name":"test"}'])
        messages = []
        async for msg in read_messages(stream):
            messages.append(msg)
        assert len(messages) == 1
        assert messages[0]["type"] == "ready"
        assert messages[0]["pid"] == 1

    @pytest.mark.asyncio
    async def test_reads_multiple_messages(self, make_stream) -> None:
        stream = make_stream([
            '{"type":"ready","pid":1,"agent_name":"test"}',
            '{"type":"progress","task_id":"t1","event_type":"checkpoint","content":"step 1"}',
            '{"type":"task_complete","task_id":"t1","status":"review","comment":"done","token_cost":0.5,"session_id":"s1"}',
        ])
        messages = []
        async for msg in read_messages(stream):
            messages.append(msg)
        assert len(messages) == 3
        assert messages[0]["type"] == "ready"
        assert messages[1]["type"] == "progress"
        assert messages[2]["type"] == "task_complete"

    @pytest.mark.asyncio
    async def test_skips_blank_lines(self) -> None:
        reader = asyncio.StreamReader()
        data = b'{"type":"ping"}\n\n\n{"type":"pong"}\n'
        reader.feed_data(data)
        reader.feed_eof()
        messages = []
        async for msg in read_messages(reader):
            messages.append(msg)
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_skips_malformed_json(self) -> None:
        reader = asyncio.StreamReader()
        data = b'{"type":"ping"}\nNOT JSON\n{"type":"pong"}\n'
        reader.feed_data(data)
        reader.feed_eof()
        messages = []
        async for msg in read_messages(reader):
            messages.append(msg)
        assert len(messages) == 2
        assert messages[0]["type"] == "ping"
        assert messages[1]["type"] == "pong"

    @pytest.mark.asyncio
    async def test_stops_on_eof(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"ready","pid":1,"agent_name":"t"}\n')
        reader.feed_eof()
        messages = []
        async for msg in read_messages(reader):
            messages.append(msg)
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        messages = []
        async for msg in read_messages(reader):
            messages.append(msg)
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_handles_stray_print_output(self) -> None:
        """Simulates a library printing to stdout (common in Python)."""
        reader = asyncio.StreamReader()
        data = (
            b'WARNING: something happened\n'
            b'{"type":"ready","pid":1,"agent_name":"test"}\n'
            b'DEBUG: extra output\n'
            b'{"type":"pong"}\n'
        )
        reader.feed_data(data)
        reader.feed_eof()
        messages = []
        async for msg in read_messages(reader):
            messages.append(msg)
        assert len(messages) == 2
        assert messages[0]["type"] == "ready"
        assert messages[1]["type"] == "pong"


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


class TestProtocolIntegration:
    """Higher-level tests that exercise the protocol end-to-end."""

    def test_full_worker_conversation(self) -> None:
        """Simulate a full worker task lifecycle via serialize/deserialize."""
        # 1. Orchestrator sends assign_task
        assign = serialize(AssignTaskMessage(
            task_id="uuid-t1",
            readable_id="WR-001.T01",
            title="Research Python developers",
            brief={"goal": "Find 10 senior Python devs in Colombia"},
            agent_config={"name": "analyst", "model": "claude-sonnet-4-6"},
        ))

        # 2. Agent responds with progress
        progress = serialize(ProgressMessage(
            task_id="uuid-t1",
            event_type="checkpoint",
            content="Searching web for Python developer profiles...",
        ))

        # 3. Agent makes a tool call
        tool_call = serialize(ToolCallMessage(
            request_id="req-001",
            tool="kanban.add_activity",
            params={"task_id": "uuid-t1", "content": "Found 5 candidates"},
        ))

        # 4. Orchestrator responds to tool call
        tool_resp = serialize(ToolResponseMessage(
            request_id="req-001",
            result={"success": True},
        ))

        # 5. Agent completes task
        complete = serialize(TaskCompleteMessage(
            task_id="uuid-t1",
            status="review",
            comment="Found 10 candidates, results in /workspace/outputs/candidates.json",
            token_cost=0.42,
            session_id="session-analyst-001",
        ))

        # Verify all messages are valid single-line JSON
        for line in [assign, progress, tool_call, tool_resp, complete]:
            assert "\n" not in line
            parsed = deserialize(line)
            assert "type" in parsed

        # Verify tool call correlation
        call_parsed = deserialize(tool_call)
        resp_parsed = deserialize(tool_resp)
        assert call_parsed["request_id"] == resp_parsed["request_id"]

    def test_full_manager_conversation(self) -> None:
        """Simulate a Manager chat message lifecycle."""
        # 1. Orchestrator sends chat_message
        chat = serialize(ChatMessage(
            context_key="workstream:uuid-ws",
            content="I need to hire Python developers in Colombia",
            conversation_id="conv-001",
            session_id="session-manager-ws",
        ))

        # 2. Manager streams response chunks
        chunk1 = serialize(ResponseChunkMessage(
            conversation_id="conv-001",
            context_key="workstream:uuid-ws",
            content="I'll help you with that. Let me ",
        ))
        chunk2 = serialize(ResponseChunkMessage(
            conversation_id="conv-001",
            context_key="workstream:uuid-ws",
            content="create a research task for the Analyst.",
        ))

        # 3. Manager sends final message
        final = serialize(ResponseFinalMessage(
            conversation_id="conv-001",
            context_key="workstream:uuid-ws",
            token_cost=0.08,
            session_id="session-manager-ws",
        ))

        # Verify conversation_id is consistent
        for line in [chat, chunk1, chunk2, final]:
            parsed = deserialize(line)
            assert parsed["conversation_id"] == "conv-001"

    def test_heartbeat_round_trip(self) -> None:
        """Ping/pong round trip."""
        ping = serialize(PingMessage())
        pong = serialize(PongMessage())
        assert deserialize(ping)["type"] == "ping"
        assert deserialize(pong)["type"] == "pong"
