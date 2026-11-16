"""Token counts of the local Claude Code session logs.

``<claude config>/projects/**/*.jsonl`` holds one line per event of a
session; an assistant message carries a ``usage`` object. A subagent writes
its own file and its lines carry an ``agentId``.

Only the timestamp, the ids (message, request, session, agent), the model
name and the integer token counts of a line are read. No message text is
kept, and no path of a project.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from claude_swap import token_model

TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("cacheWrite", "cache_creation_input_tokens"),
    ("cacheRead", "cache_read_input_tokens"),
    ("output", "output_tokens"),
)


def parse_ts(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude_projects_dir(env=None) -> Path:
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def read_messages(projects_dir: Path, since: float, until: float | None = None) -> list[dict]:
    """Assistant messages with ``since < time <= until``, oldest first.

    Each is ``{"ts", "model", "session", "agent", "tokens": {input,
    cacheWrite, cacheRead, output}, "weighted"}``. A message repeats on
    several lines of a log: one (message id, request id) counts once. Files
    not changed since ``since`` are skipped.
    """
    found: dict[tuple, dict] = {}
    try:
        files = [p for p in Path(projects_dir).rglob("*.jsonl") if p.stat().st_mtime >= since]
    except OSError:
        return []
    for path in files:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                message = row.get("message") if isinstance(row, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                ts = parse_ts(row.get("timestamp")) if isinstance(row, dict) else None
                if not isinstance(usage, dict) or ts is None or ts <= since:
                    continue
                if until is not None and ts > until:
                    continue
                key = (message.get("id"), row.get("requestId"))
                if key == (None, None):
                    key = (str(path), ts)
                model = message.get("model") if isinstance(message.get("model"), str) else None
                found[key] = {
                    "ts": ts,
                    "model": model,
                    "session": row.get("sessionId") if isinstance(row.get("sessionId"), str) else None,
                    "agent": row.get("agentId") if isinstance(row.get("agentId"), str) else None,
                    "tokens": {name: token_model._count(usage, key_) for name, key_ in TOKEN_KEYS},
                    "weighted": token_model.weighted_tokens(model, usage),
                }
    return sorted(found.values(), key=lambda m: m["ts"])


def summarize(messages: list[dict]) -> dict:
    """Sums of a list of messages: by token type, weighted, and by model."""
    by_type = {name: 0 for name, _ in TOKEN_KEYS}
    models: dict[str, dict] = {}
    weighted = 0.0
    for message in messages:
        for name in by_type:
            by_type[name] += message["tokens"][name]
        weighted += message["weighted"]
        row = models.setdefault(message["model"] or "unknown", {"messages": 0, "weightedTokens": 0.0})
        row["messages"] += 1
        row["weightedTokens"] += message["weighted"]
    for row in models.values():
        row["weightedTokens"] = round(row["weightedTokens"])
    return {
        "messages": len(messages),
        "tokens": by_type,
        "weightedTokens": round(weighted),
        "models": {name: models[name] for name in sorted(models)},
    }


def select(messages: list[dict], sessions=None, agents=None) -> list[dict]:
    """Messages of the given session ids and/or agent ids. Empty filters keep all.

    With ``agents`` only the lines of those subagents count. With ``sessions``
    and no ``agents``, every line of those sessions counts, subagents too.
    """
    sessions, agents = set(sessions or ()), set(agents or ())
    if agents:
        return [m for m in messages if m["agent"] in agents and (not sessions or m["session"] in sessions)]
    if sessions:
        return [m for m in messages if m["session"] in sessions]
    return list(messages)
