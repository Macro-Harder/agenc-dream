#!/usr/bin/env python3
"""Select allowlisted AgenC rollout sessions; enforces path policy.

Only policy reader. Import contract: select_sessions(...) -> list[dict]
manifest rows. Prints that JSON and nothing else when run as a script.
Does not open a file whose dirname is denied. No model calls.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOW = ROOT / "policy" / "allow.json"
DEFAULT_DENY = ROOT / "policy" / "deny.json"
_ID_RE = re.compile(r"^(conv-[A-Za-z0-9]+|session_[A-Za-z0-9-]+)$")
_MAX_LINE_CAP = 2_000_000


def _load_policy(allow_path=None, deny_path=None):
    allow = json.loads(Path(str(allow_path or DEFAULT_ALLOW)).read_text())
    deny = json.loads(Path(str(deny_path or DEFAULT_DENY)).read_text())
    prefixes = [str(x).lower() for x in allow.get("prefixes", [])]
    subs = [str(x).lower() for x in deny.get("substrings", [])]
    return prefixes, subs


def _deny_hit(text: str, subs) -> bool:
    low = text.lower()
    return any(s in low for s in subs)


def _allow_hit(text: str, prefixes) -> bool:
    low = text.lower()
    return any(p in low for p in prefixes)


def _normalize_ts(value: str) -> str:
    if not value:
        return ""
    v = value.strip().replace("+00:00", "Z")
    if v.endswith("Z"):
        v2 = v[:-1]
    else:
        v2 = v
    # Accept YYYY-MM-DDTHH:MM:SS[.fff][Z]; drop fraction.
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", v2)
    if m:
        return m.group(1) + "Z"
    return v


def _iter_project_dirs(base):
    base = Path(str(base))
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if child.is_dir():
            yield child


def _session_files(session_dir: Path):
    try:
        return sorted(p for p in session_dir.glob("rollout-*.jsonl") if p.is_file())
    except OSError:
        return []


def _scan_meta(path: Path):
    """Return (sessionId, cwd, model, timestamp, all_session_ids) or None.

    Reads lines until the first session_meta only; does not keep bodies
    beyond what is needed for the meta scan. A second pass for run_terminal
    is a separate limited scan.
    """
    ids: list[str] = []
    first = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(line) > _MAX_LINE_CAP:
                    continue
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                sid = str(payload.get("sessionId") or payload.get("session_id") or "")
                ids.append(sid)
                if first is None:
                    first = (
                        sid,
                        str(payload.get("cwd") or ""),
                        str(payload.get("model") or ""),
                        str(payload.get("timestamp") or ""),
                    )
                # Keep scanning for a second meta to detect disagreement;
                # a third pass would also do, but two is enough to bad-meta.
                if len(ids) >= 2:
                    break
    except OSError:
        return None
    if first is None:
        return None
    return (first[0], first[1], first[2], first[3], ids)


def _has_run_terminal(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(line) > _MAX_LINE_CAP:
                    continue
                s = line.strip()
                if not s or "run_terminal" not in s:
                    continue
                try:
                    rec = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and _is_terminal(rec):
                    return True
    except OSError:
        return False
    return False


def _is_terminal(rec: dict) -> bool:
    if rec.get("type") == "run_terminal":
        return True
    payload = rec.get("payload") or {}
    if isinstance(payload, dict):
        msg = payload.get("msg") or {}
        if isinstance(msg, dict) and msg.get("type") == "run_terminal":
            return True
    return False


def _row(session_id, rollout, cwd, project, created, action, reason=None):
    row = {
        "sessionId": session_id,
        "rollout": rollout,
        "cwd": cwd,
        "project": project,
        "created": created,
        "action": action,
    }
    if reason is not None:
        row["reason"] = reason
    return row


def select_sessions(base=None, since="", ingested_ids=(), allow_path=None,
                    deny_path=None, cap=20):
    """Scan base/<project>/sessions/<conv>/rollout-*.jsonl and classify.

    base defaults to ~/.agenc/projects. Rows carry manifest keys.
    """
    base = Path(os.path.expanduser(str(base or "~/.agenc/projects")))
    prefixes, subs = _load_policy(allow_path, deny_path)
    ingested = set(ingested_ids or ())
    since_n = _normalize_ts(since) if since else ""
    rows: list[dict] = []
    analyze_count = 0

    for project in _iter_project_dirs(base):
        dirname = project.name
        sessions = project / "sessions"
        if not sessions.is_dir():
            continue
        for session_dir in sorted(sessions.iterdir()):
            if not session_dir.is_dir():
                continue
            conv = session_dir.name
            files = _session_files(session_dir)
            if not files:
                continue
            # Step 1: dirname deny without opening.
            if _deny_hit(dirname, subs):
                rows.append(_row(conv, str(files[0]), "", dirname, "", "deny", "deny"))
                continue
            dirname_allow = _allow_hit(dirname, prefixes)

            # Step 6: pick lexicographically last finished file among
            # dirname-eligible ones; one row per session dir.
            chosen = None
            for candidate in files:  # lexicographic order
                if _has_run_terminal(candidate):
                    chosen = candidate  # later wins
            finished = chosen is not None
            target = chosen if finished else files[-1]

            meta = _scan_meta(target)
            if meta is None:
                rows.append(_row(conv, str(target), "", dirname, "", "bad-meta", "bad-meta"))
                continue
            session_id, cwd, model, timestamp, ids = meta
            created = _normalize_ts(timestamp) if timestamp else ""

            # Step 5: disagreement between metas is bad-meta.
            uniq = {i for i in ids if i}
            if len(uniq) > 1:
                rows.append(_row(session_id, str(target), cwd, dirname, created, "bad-meta", "bad-meta"))
                continue

            # Steps 3-4: cwd + dirname policy.
            if _deny_hit(cwd, subs) or _deny_hit(dirname, subs):
                rows.append(_row(session_id, str(target), cwd, dirname, created, "deny", "deny"))
                continue
            if not (dirname_allow or _allow_hit(cwd, prefixes)):
                rows.append(_row(session_id, str(target), cwd, dirname, created, "deny", "not-allow"))
                continue

            # bad-id is a render-time gate too; surface it here for the
            # caller, but render_rollout enforces it again.
            if not _ID_RE.match(session_id):
                rows.append(_row(session_id, str(target), cwd, dirname, created, "bad-id", "bad-id"))
                continue

            if not finished:
                rows.append(_row(session_id, str(target), cwd, dirname, created, "running", "running"))
                continue

            # Window and dedup; runs at or after since, oldest first is a
            # caller sort. Here count toward cap and defer overflow.
            if since_n and created and created < since_n:
                continue
            if session_id in ingested:
                continue
            if cap is not None and analyze_count >= cap:
                rows.append(_row(session_id, str(target), cwd, dirname, created, "deferred", "cap"))
                continue
            analyze_count += 1
            rows.append(_row(session_id, str(target), cwd, dirname, created, "analyze"))

    # Oldest first among analyze/deferred; keep other rows in scan order.
    analyze = [r for r in rows if r["action"] == "analyze"]
    rest = [r for r in rows if r["action"] != "analyze"]
    analyze.sort(key=lambda r: (r["created"], r["sessionId"]))
    return analyze + rest


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="select allowlisted sessions")
    ap.add_argument("--base", default=None)
    ap.add_argument("--since", default="")
    ap.add_argument("--cap", type=int, default=20)
    ap.add_argument("--allow", default=None)
    ap.add_argument("--deny", default=None)
    args = ap.parse_args()
    print(json.dumps(select_sessions(base=args.base, since=args.since,
                                     cap=args.cap, allow_path=args.allow,
                                     deny_path=args.deny)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
