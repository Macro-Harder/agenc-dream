"""Unit tests for select_sessions policy (no model)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from select_sessions import select_sessions


def _meta(session_id="conv-aaa1", cwd="/workspace/agenc-public/brainstew",
          ts="2026-11-04T00:00:00Z"):
    return {"type": "session_meta", "payload": {"sessionId": session_id, "cwd": cwd, "model": "m", "timestamp": ts}}


def _term():
    return {"type": "event_msg", "payload": {"msg": {"type": "run_terminal", "payload": {}}}}


def _make(base, project, conv, records, files=("rollout-a.jsonl",)):
    d = base / project / "sessions" / conv
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in files:
        p = d / name
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        paths.append(p)
    return paths


def test_allow_brainstew(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-aaa1", [_meta(), _term()])
    rows = select_sessions(base=tmp_path, since="", cap=20)
    assert rows and rows[0]["action"] == "analyze"


def test_allow_agenc_public(tmp_path):
    _make(tmp_path, "agenc-public-proj", "conv-bbb1",
          [_meta(session_id="conv-bbb1", cwd="/workspace/agenc-public/x"), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "analyze"


def test_deny_wins_gf2_in_temp_tree(tmp_path):
    # temp-tree session_meta with cwd /tmp/gf2-rebuild/x is deny, no render
    _make(tmp_path, "brainstew-proj", "conv-gf01",
          [_meta(session_id="conv-gf01", cwd="/tmp/gf2-rebuild/x"), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "deny"
    assert rows[0]["reason"] == "deny"


def test_dirname_brainstew_and_redpytest_denied(tmp_path):
    _make(tmp_path, "brainstew-redpytest-proj", "conv-ccc1", [_meta(), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "deny"
    assert rows[0]["reason"] == "deny"


def test_self_deny_agenc_dream(tmp_path):
    _make(tmp_path, "my-agenc-dream-proj", "conv-ddd1", [_meta(), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "deny"


def test_not_allow(tmp_path):
    _make(tmp_path, "random-proj", "conv-eee1",
          [_meta(session_id="conv-eee1", cwd="/somewhere/else"), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "deny"
    assert rows[0]["reason"] == "not-allow"


def test_running_without_terminal(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-fff1", [_meta(session_id="conv-fff1")])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "running"
    assert rows[0]["reason"] == "running"


def test_multi_meta_disagreement_bad_meta(tmp_path):
    m1 = _meta(session_id="conv-ggg1")
    m2 = _meta(session_id="conv-other")
    _make(tmp_path, "brainstew-proj", "conv-ggg1", [m1, m2, _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "bad-meta"


def test_created_from_meta(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-hhh1",
          [_meta(session_id="conv-hhh1", ts="2026-11-04T12:34:56.789+00:00"), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["created"] == "2026-11-04T12:34:56Z"


def test_since_filter(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-iii1",
          [_meta(session_id="conv-iii1", ts="2026-11-01T00:00:00Z"), _term()])
    rows = select_sessions(base=tmp_path, since="2026-11-04T00:00:00Z")
    assert rows == []


def test_ingested_skipped(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-jjj1",
          [_meta(session_id="conv-jjj1"), _term()])
    rows = select_sessions(base=tmp_path, ingested_ids=["conv-jjj1"])
    assert rows == []


def test_bad_id_action(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-bad",
          [_meta(session_id="bad id!"), _term()])
    rows = select_sessions(base=tmp_path)
    assert rows[0]["action"] == "bad-id"


def test_cap_deferred(tmp_path):
    for i, n in enumerate(["conv-001", "conv-002", "conv-003"]):
        _make(tmp_path, "brainstew-proj", n,
              [_meta(session_id=n, ts=f"2026-11-04T00:00:0{i}Z"), _term()])
    rows = select_sessions(base=tmp_path, cap=2)
    actions = [r["action"] for r in rows]
    assert actions.count("analyze") == 2
    assert "deferred" in actions
    # analyze rows come oldest-first
    analyzed = [r for r in rows if r["action"] == "analyze"]
    assert analyzed == sorted(analyzed, key=lambda r: (r["created"], r["sessionId"]))


def test_deferred_not_in_skipped_semantics(tmp_path):
    for i, n in enumerate(["conv-001", "conv-002"]):
        _make(tmp_path, "brainstew-proj", n,
              [_meta(session_id=n, ts=f"2026-11-04T00:00:0{i}Z"), _term()])
    rows = select_sessions(base=tmp_path, cap=1)
    deferred = [r for r in rows if r["action"] == "deferred"]
    assert len(deferred) == 1
    assert deferred[0].get("reason") == "cap"


def test_analyze_row_has_no_reason(tmp_path):
    _make(tmp_path, "brainstew-proj", "conv-kkk1",
          [_meta(session_id="conv-kkk1"), _term()])
    row = select_sessions(base=tmp_path)[0]
    assert "reason" not in row


def test_policy_files_loadable():
    root = Path(__file__).resolve().parents[1]
    allow = json.loads((root / "policy" / "allow.json").read_text())
    deny = json.loads((root / "policy" / "deny.json").read_text())
    assert allow["prefixes"] == ["brainstew", "agenc-public"]
    for s in ["gf2", "g05", "agenc-hnic", "hnic", "redteam", "redpytest",
              "harness", "desk-lib/agenc", "agenc-skills-local/agenc-lead",
              "child_broker", "relay.py", "private", ".env", "secret",
              "agenc-dream"]:
        assert s in deny["substrings"]
