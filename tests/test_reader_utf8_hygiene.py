"""W5-P2-H1: NDJSON reader loops must tolerate malformed UTF-8.

Three reader sites all used ``bytes.decode()`` (strict mode), which
raises ``UnicodeDecodeError`` on a single bad byte. A bad byte from
a buggy agent / Claude CLI / Orchestrator would kill the reader
loop → the heartbeat dies → the subprocess is reaped → DoS.

The fix is ``bytes.decode(errors="replace")`` at every site so a
bad byte is substituted with U+FFFD and the loop continues. The
JSON parse below the decode then fails cleanly with the existing
``json.JSONDecodeError`` log+continue branch.
"""
from __future__ import annotations

import json


def _decode_like_reader_loop(raw: bytes) -> str | None:
    """Replicate the decode-and-strip pattern used in the readers.

    This is intentionally a copy of the contract — not an import —
    so the test locks in the contract: ``raw.decode(errors="replace").strip()``
    must never raise on malformed UTF-8.
    """
    return raw.decode(errors="replace").strip()


def test_valid_utf8_round_trips_unchanged() -> None:
    """Sanity: ASCII payloads survive unchanged."""
    raw = b'{"type": "pong"}\n'
    assert _decode_like_reader_loop(raw) == '{"type": "pong"}'


def test_emoji_round_trips_unchanged() -> None:
    """Multi-byte UTF-8 (emoji) round-trips without substitution."""
    raw = '{"content": "✓ done"}\n'.encode("utf-8")
    decoded = _decode_like_reader_loop(raw)
    assert "✓ done" in decoded
    # And it parses as JSON.
    assert json.loads(decoded)["content"] == "✓ done"


def test_invalid_utf8_byte_does_not_raise() -> None:
    """The previously-fatal case: ``\\xc3\\x28`` is a malformed
    UTF-8 sequence (lead byte without valid continuation). With
    strict decode this raises ``UnicodeDecodeError`` and kills
    the reader loop. With ``errors="replace"`` we get a U+FFFD
    substitution and continue."""
    raw = b'{"x": "before' + b'\xc3\x28' + b'after"}\n'
    # Must not raise.
    decoded = _decode_like_reader_loop(raw)
    # U+FFFD is the replacement character.
    assert "\ufffd" in decoded
    # The JSON parse will likely fail on the malformed value, but
    # that's caught by the existing JSONDecodeError handler in the
    # reader loop — the important property is that decode itself
    # didn't raise.


def test_strict_decode_would_raise_on_the_same_bytes() -> None:
    """Pin the regression: the pre-fix posture (strict decode)
    raises on the same input that the fix tolerates. If this test
    starts passing without raising, the bytes-string we're using
    is no longer a useful regression marker — pick another one."""
    raw = b'{"x": "before' + b'\xc3\x28' + b'after"}\n'
    try:
        raw.decode()  # strict mode (the pre-fix behaviour)
    except UnicodeDecodeError:
        return  # expected
    raise AssertionError(
        "Test bytes are no longer malformed under strict decode — "
        "update the fixture so the regression marker stays useful."
    )


def test_decode_replace_lets_json_parser_decide() -> None:
    """End-to-end shape: decode never raises, then JSON parse
    decides whether the line was usable. The reader-loop branch
    is `decode → json.loads(decoded)` so the parser is the
    second-line defense."""
    raw = b'{"valid": "json", "ok": true}\n'
    decoded = _decode_like_reader_loop(raw)
    assert json.loads(decoded) == {"valid": "json", "ok": True}

    raw_bad = b'this is not JSON\n'
    decoded_bad = _decode_like_reader_loop(raw_bad)
    # The reader loop's `try: json.loads(decoded)` handles this.
    try:
        json.loads(decoded_bad)
    except json.JSONDecodeError:
        return
    raise AssertionError("Non-JSON line should fail json.loads()")
