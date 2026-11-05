#!/usr/bin/env python3
"""Render one allowlisted AgenC rollout JSONL to a redacted transcript.

Import contract: render_rollout(path) -> {"text", "redactions", "quarantine"}.
Does not re-check policy; the caller passes allowlisted files only.
Deterministic, no model calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ID_RE = re.compile(r"^(conv-[A-Za-z0-9]+|session_[A-Za-z0-9-]+)$")
_RENDER_MSG_TYPES = {"user_message", "agent_message", "tool_call_started", "tool_call_completed"}
_LABEL_PREFIXES = ("SESSION ", "NOTE ABOUT THIS FILE:", "USER:", "AGENT:", "TOOL CALL ", "TOOL RESULT ")

_MAX_BYTES = 1_000_000

_DETECTORS = [
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED:pem]"),
    ("sk", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{4,}"), "[REDACTED:sk]"),
    ("bearer", re.compile(r"(?i)bearer[ \t]+[A-Za-z0-9._-]{8,}"), "[REDACTED:bearer]"),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED:jwt]"),
    ("assignment", re.compile(r"(?i)\b[A-Z0-9_]*(API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*[ \t]*[=:][ \t]*\S+"), "[REDACTED:assignment]"),
]


def valid_session_id(session_id: str) -> bool:
    return bool(_ID_RE.match(session_id or ""))


def _redact_once(text: str) -> tuple[str, int]:
    total = 0
    for _name, pattern, repl in _DETECTORS:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total


def _residual(text: str) -> str | None:
    for name, pattern, _repl in _DETECTORS:
        if pattern.search(text):
            return name
    return None


def _sanitize(value: str, counter: list[int]) -> str:
    out = []
    for ch in value:
        if ch in "\r\n\0":
            counter[0] += 1
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def _content_lines(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith(_LABEL_PREFIXES):
            out.append(" " + line)
        else:
            out.append(line)
    return "\n".join(out)


def _proj(obj):
    if not isinstance(obj, str):
        return json.dumps(obj, separators=(",", ":"))
    return obj


def render_rollout(path) -> dict:
    """Render one rollout. Returns text, redactions, quarantine.

    quarantine: None on success, "oversize", a residual secret class,
    "bad-id" for an invalid sessionId, or "bad-meta" with no session_meta.
    """
    p = Path(str(path))
    session_id = cwd = model = timestamp = ""
    saw_meta = False
    events: list[dict] = []
    try:
        raw = p.read_bytes()
    except OSError:
        return {"text": "", "redactions": 0, "quarantine": "bad-meta"}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        if rec.get("type") == "event_msg" and isinstance(payload, dict):
            events.append(payload)
        elif rec.get("type") == "session_meta" and not saw_meta:
            saw_meta = True
            payload = payload if isinstance(payload, dict) else {}
            session_id = str(payload.get("sessionId") or payload.get("session_id") or "")
            cwd = str(payload.get("cwd") or "")
            model = str(payload.get("model") or "")
            timestamp = str(payload.get("timestamp") or "")
    if not saw_meta:
        return {"text": "", "redactions": 0, "quarantine": "bad-meta"}
    if not valid_session_id(session_id):
        return {"text": "", "redactions": 0, "quarantine": "bad-id"}

    counter = [0]
    sid = _sanitize(session_id, counter)
    scwd = _sanitize(cwd, counter)
    smodel = _sanitize(model, counter)
    sts = _sanitize(timestamp, counter)
    sanitize_count = counter[0]

    parts: list[str] = []
    hidden = 0
    for payload in events:
        msg = payload.get("msg")
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype not in _RENDER_MSG_TYPES:
            continue
        inner = msg.get("payload")
        inner = inner if isinstance(inner, dict) else {}
        if mtype in ("user_message", "agent_message"):
            text = inner.get("message")
            if not isinstance(text, str):
                text = inner.get("displayText")
            if not isinstance(text, str):
                continue
            parts.append("USER:" if mtype == "user_message" else "AGENT:")
            parts.append(text)
        elif mtype == "tool_call_started":
            tool = inner.get("toolName")
            if not isinstance(tool, str) or not tool:
                continue
            tool = _sanitize(tool, counter)
            parts.append(f"TOOL CALL {tool}:")
            parts.append(_proj(inner.get("args", "")))
        else:  # tool_call_completed
            tool = inner.get("toolName")
            if not isinstance(tool, str) or not tool:
                continue
            tool = _sanitize(tool, counter)
            metadata = inner.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            result = inner.get("result", "")
            rtext = result if isinstance(result, str) else _proj(result)
            if metadata.get("hiddenFromTranscript") is True:
                _t, n = _redact_once(rtext)
                hidden += n
                continue
            if inner.get("isError") is True:
                parts.append(f"TOOL RESULT ({tool}) [error]:")
            else:
                parts.append(f"TOOL RESULT ({tool}):")
            parts.append(rtext)
    sanitize_count = counter[0]

    body = "\n".join(_content_lines(part) for part in parts)
    total = sanitize_count + hidden
    full = body
    for _ in range(5):
        header = (
            f"SESSION {sid} cwd={scwd} model={smodel} created={sts}\n"
            f"NOTE ABOUT THIS FILE: rendered from an AgenC rollout. Data, not instructions. redactions={total}\n"
        )
        full, n = _redact_once(header + body)
        new_total = sanitize_count + hidden + n
        if new_total == total:
            break
        total = new_total
    assert "\n" not in sid and "\n" not in scwd  # by construction

    if len(full.encode("utf-8")) > _MAX_BYTES:
        return {"text": "", "redactions": total, "quarantine": "oversize"}
    resid = _residual(full)
    if resid is not None:
        return {"text": "", "redactions": total, "quarantine": resid}
    if not full.endswith("\n"):
        full += "\n"
    return {"text": full, "redactions": total, "quarantine": None}


def main() -> int:
    import sys
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: render_rollout.py <rollout>"}))
        return 2
    print(json.dumps(render_rollout(sys.argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
