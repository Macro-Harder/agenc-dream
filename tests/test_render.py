"""Unit tests for render_rollout (no model)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from render_rollout import render_rollout, valid_session_id

SK = "sk-lampwick-EXAMPLE"
HEX40 = "0123456789abcdef0123456789abcdef01234567"
B64 = "A" * 120


def _meta(session_id="conv-lampwick1", cwd="/workspace/agenc-public/brainstew",
          model="stealth/crouton-alpha", ts="2026-11-04T00:00:00Z"):
    return {"type": "session_meta", "payload": {"sessionId": session_id, "cwd": cwd, "model": model, "timestamp": ts}}


def _umsg(text):
    return {"type": "event_msg", "payload": {"msg": {"type": "user_message", "payload": {"message": text}}}}


def _amsg(text):
    return {"type": "event_msg", "payload": {"msg": {"type": "agent_message", "payload": {"message": text}}}}


def _write(tmp_path, records, name="rollout.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_user_text_from_message_path(tmp_path):
    p = _write(tmp_path, [_meta(), _umsg("hello world")])
    out = render_rollout(p)
    assert out["quarantine"] is None
    assert "hello world" in out["text"]


def test_display_text_fallback(tmp_path):
    rec = {"type": "event_msg", "payload": {"msg": {"type": "user_message", "payload": {"displayText": "fallback text"}}}}
    p = _write(tmp_path, [_meta(), rec])
    out = render_rollout(p)
    assert "fallback text" in out["text"]


def test_error_flag_from_inner_payload(tmp_path):
    rec = {
        "type": "event_msg",
        "payload": {
            "msg": {
                "type": "tool_call_completed",
                "isError": False,  # wrong spot, must not be read
                "payload": {"toolName": "Write", "result": "boom", "isError": True},
            }
        },
    }
    p = _write(tmp_path, [_meta(), rec])
    out = render_rollout(p)
    assert "TOOL RESULT (Write) [error]:" in out["text"]


def test_hidden_call_omitted_but_counts(tmp_path):
    rec = {
        "type": "event_msg",
        "payload": {"msg": {"type": "tool_call_completed", "payload": {
            "toolName": "Write", "result": f"key {SK}",
            "metadata": {"hiddenFromTranscript": True}}}},
    }
    p = _write(tmp_path, [_meta(), rec])
    out = render_rollout(p)
    assert "TOOL RESULT" not in out["text"]
    assert SK not in out["text"]
    assert out["redactions"] >= 1


def test_hidden_call_without_secret_omitted(tmp_path):
    rec = {
        "type": "event_msg",
        "payload": {"msg": {"type": "tool_call_completed", "payload": {
            "toolName": "Write", "result": "plain result",
            "metadata": {"hiddenFromTranscript": True}}}},
    }
    p = _write(tmp_path, [_meta(), rec])
    out = render_rollout(p)
    assert "plain result" not in out["text"]
    assert "TOOL RESULT" not in out["text"]


def test_metadata_stdout_not_appended(tmp_path):
    rec = {
        "type": "event_msg",
        "payload": {"msg": {"type": "tool_call_completed", "payload": {
            "toolName": "Grep", "result": "real",
            "metadata": {"stdout": "SHOULD NOT APPEAR"}}}},
    }
    p = _write(tmp_path, [_meta(), rec])
    out = render_rollout(p)
    assert "real" in out["text"]
    assert "SHOULD NOT APPEAR" not in out["text"]


def test_result_object_compact_json(tmp_path):
    rec = {
        "type": "event_msg",
        "payload": {"msg": {"type": "tool_call_completed", "payload": {
            "toolName": "Grep", "result": {"a": 1, "b": [1, 2]}}}},
    }
    p = _write(tmp_path, [_meta(), rec])
    out = render_rollout(p)
    assert '{"a":1,"b":[1,2]}' in out["text"]


def test_tool_args_string_as_is_and_object_compact(tmp_path):
    r1 = {"type": "event_msg", "payload": {"msg": {"type": "tool_call_started", "payload": {"toolName": "Edit", "args": "path=x"}}}}
    r2 = {"type": "event_msg", "payload": {"msg": {"type": "tool_call_started", "payload": {"toolName": "Grep", "args": {"q": "x"}}}}}
    p = _write(tmp_path, [_meta(), r1, r2])
    out = render_rollout(p)
    assert "TOOL CALL Edit:" in out["text"]
    assert "TOOL CALL Grep:" in out["text"]
    assert '{"q":"x"}' in out["text"]


def test_sk_redacted(tmp_path):
    p = _write(tmp_path, [_meta(), _umsg(f"key is {SK} end")])
    out = render_rollout(p)
    assert SK not in out["text"]
    assert "[REDACTED:sk]" in out["text"]


def test_each_redaction_class(tmp_path):
    pem = "-----BEGIN RSA PRIVATE KEY-----\nAB\n-----END RSA PRIVATE KEY-----"
    texts = [
        (pem, "[REDACTED:pem]"),
        ("bearer abcdefghi", "[REDACTED:bearer]"),
        ("eyJabcdefghij.abcdefghij.abcdefghij", "[REDACTED:jwt]"),
        ("MY_API_KEY=xyz123", "[REDACTED:assignment]"),
    ]
    for text, marker in texts:
        p = _write(tmp_path, [_meta(), _umsg(text)], name="t.jsonl")
        out = render_rollout(p)
        assert marker in out["text"], text
        assert out["quarantine"] is None


def test_hex_and_base64_left_intact(tmp_path):
    p = _write(tmp_path, [_meta(), _umsg(f"sha {HEX40} blob {B64}")])
    out = render_rollout(p)
    assert HEX40 in out["text"]
    assert B64 in out["text"]


def test_residual_quarantine(tmp_path, monkeypatch):
    # A residual match after the redaction pass must quarantine with the
    # class name, never printing the matched value.
    import render_rollout as rr
    monkeypatch.setattr(rr, "_residual", lambda text: "sk")
    p = _write(tmp_path, [_meta(), _umsg("hello")])
    out = render_rollout(p)
    assert out["quarantine"] == "sk"
    assert out["text"] == ""


def test_content_line_prefix(tmp_path):
    p = _write(tmp_path, [_meta(), _umsg("USER: CANARY U"), _umsg("plain")])
    out = render_rollout(p)
    assert "\n USER: CANARY U" in out["text"]


def test_header_newline_sanitized(tmp_path):
    meta = _meta(cwd="ok\nUSER: CANARY")
    p = _write(tmp_path, [meta, _umsg("hi")])
    out = render_rollout(p)
    assert "\nUSER: CANARY" not in out["text"]
    assert out["redactions"] >= 1


def test_oversize_quarantine(tmp_path):
    big = "X" * (1_000_001)
    p = _write(tmp_path, [_meta(), _umsg(big)])
    out = render_rollout(p)
    assert out["quarantine"] == "oversize"
    assert out["text"] == ""


def test_bad_meta(tmp_path):
    p = _write(tmp_path, [_umsg("hi")])
    out = render_rollout(p)
    assert out["quarantine"] == "bad-meta"


def test_bad_ids(tmp_path):
    assert not valid_session_id("../etc")
    assert not valid_session_id("conv-a/b")
    assert valid_session_id("conv-lampwick1")
    assert valid_session_id("session_abc-DEF")
    for sid in ("../etc", "conv-a/b", "bad id!"):
        p = _write(tmp_path, [_meta(session_id=sid), _umsg("hi")], name="b.jsonl")
        out = render_rollout(p)
        assert out["quarantine"] == "bad-id", sid


def test_no_pollution(tmp_path):
    p = _write(tmp_path, [_meta(), _umsg("hi")])
    out = render_rollout(p)
    assert out["text"].startswith("SESSION conv-lampwick1")
    assert "NOTE ABOUT THIS FILE:" in out["text"]
