"""Measurement records: tasks and measured stretches of work.

Raw data is append-only JSONL in the backup root, one file per kind, never
edited: ``tasks.jsonl`` and ``measures.jsonl``. Every line carries
``schemaVersion``, ``recordedAt`` and ``source`` ("passive" here; a
controlled experiment will write "controlled"). Derived numbers are computed
on read and always come with ``n``, ``median``, ``mean``, ``p25``, ``p75``
and a ``confidence`` word.

Identity: account numbers, plan words, and the ids the caller gives (task,
agent, session, a project HASH). No email, no path, no account name. Text
fields are cut to a short safe alphabet.

The tokens of a task or a stretch come from the local Claude Code logs
(``usage_log``): the caller does not count anything.
"""

from __future__ import annotations

import json
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import budget_calibrate, token_model, usage_log

SCHEMA_VERSION = 1
TASKS_FILENAME = "tasks.jsonl"
MEASURES_FILENAME = "measures.jsonl"
MIN_N_FOR_ESTIMATE = 5
MAX_MEASURE_S = 48 * 3600.0
OUTCOMES = ("done", "failed", "stalled")

_SAFE = re.compile(r"[^A-Za-z0-9 _.:#+-]")
_HASH = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(value, limit: int = 96) -> str | None:
    """A short label with no character that could carry an email or a path."""
    if not isinstance(value, str):
        return None
    text = _SAFE.sub("", value).strip()[:limit]
    return text or None


def _ids(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return [c for c in (clean(v, 64) for v in values[:64]) if c]


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def stats(values: list[float]) -> dict:
    """``n``, ``median``, ``mean``, ``p25``, ``p75`` of a list (None when empty)."""
    values = sorted(float(v) for v in values)
    n = len(values)
    if n == 0:
        return {"n": 0, "median": None, "mean": None, "p25": None, "p75": None}
    if n == 1:
        p25 = p75 = values[0]
    else:
        quartiles = statistics.quantiles(values, n=4, method="inclusive")
        p25, p75 = quartiles[0], quartiles[2]
    return {
        "n": n,
        "median": round(statistics.median(values)),
        "mean": round(statistics.fmean(values)),
        "p25": round(p25),
        "p75": round(p75),
    }


def sample_confidence(n: int) -> str:
    if n >= 30:
        return "high"
    if n >= 10:
        return "medium"
    if n >= MIN_N_FOR_ESTIMATE:
        return "low"
    return "unknown"


# --- Percent of the active account during a stretch -----------------------


def percent_track(backup_root: Path, start: float, end: float) -> dict:
    """What the engine's polls say about the active account in ``[start, end]``.

    The first reading is the last poll at or before ``start`` (not older than
    the engine's poll gap), else the first poll inside. The percent is a
    whole number and a poll can be minutes old: ``reasons`` says when the
    track cannot be trusted.
    """
    polls = budget_calibrate.read_polls(backup_root)
    before = [p for p in polls if start - budget_calibrate.MAX_GAP_S <= p[0] <= start]
    inside = [p for p in polls if start < p[0] <= end]
    track = ([before[-1]] if before else []) + inside
    reasons = []
    if len(track) < 2:
        return {"account": None, "readings": len(track), "fiveHour": None, "sevenDay": None,
                "reasons": ["no-percent-readings"]}
    accounts = sorted({p[1] for p in track})
    if len(accounts) > 1:
        reasons.append("account-switch")

    def window(index: int, name: str) -> dict | None:
        values = [p[index] for p in track if p[1] == track[-1][1] and p[index] is not None]
        if len(values) < 2:
            return None
        if any(b < a for a, b in zip(values, values[1:])):
            reasons.append(f"{name}-reset")
        delta = values[-1] - values[0]
        if 0 <= delta < 1:
            reasons.append(f"{name}-within-resolution")
        return {"start": values[0], "end": values[-1], "delta": round(delta, 2)}

    five, week = window(2, "five-hour"), window(3, "weekly")
    return {
        "account": int(track[-1][1]) if track[-1][1].isdigit() else track[-1][1],
        "accountsSeen": [int(a) if a.isdigit() else a for a in accounts],
        "readings": len(track),
        "firstReadingAt": _iso(track[0][0]),
        "lastReadingAt": _iso(track[-1][0]),
        "fiveHour": five,
        "sevenDay": week,
        "reasons": reasons,
    }


# --- Measured stretches ---------------------------------------------------


def measure_start(backup_root: Path, now: float, label=None, sessions=None, agents=None) -> dict:
    """Open a measurement. ``sessions``/``agents`` limit the tokens to those ids."""
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "event": "start",
        "source": "passive",
        "recordedAt": _iso(now),
        "measureId": uuid.uuid4().hex[:12],
        "label": clean(label),
        "startedAt": now,
        "sessions": _ids(sessions),
        "agents": _ids(agents),
    }
    _append(Path(backup_root) / MEASURES_FILENAME, row)
    return {"measureId": row["measureId"], "startedAt": _iso(now), "label": row["label"]}


def measure_stop(backup_root: Path, projects_dir: Path, measure_id, now: float) -> dict:
    """Close a measurement: tokens from the logs, percent from the engine polls."""
    path = Path(backup_root) / MEASURES_FILENAME
    wanted = clean(measure_id, 32)
    rows = _read(path)
    start = next((r for r in reversed(rows) if r.get("event") == "start" and r.get("measureId") == wanted), None)
    if start is None:
        return {"ok": False, "error": "unknown-measure-id"}
    if any(r.get("event") == "stop" and r.get("measureId") == wanted for r in rows):
        return {"ok": False, "error": "already-stopped"}
    began = float(start["startedAt"])
    if not 0 < now - began <= MAX_MEASURE_S:
        return {"ok": False, "error": "bad-duration"}

    everything = usage_log.read_messages(projects_dir, began, now)
    chosen = usage_log.select(everything, start.get("sessions"), start.get("agents"))
    usage = usage_log.summarize(chosen)
    all_weighted = usage_log.summarize(everything)["weightedTokens"]
    track = percent_track(backup_root, began, now)

    contaminated = list(track.pop("reasons"))
    others = all_weighted - usage["weightedTokens"]
    if others > 0:
        contaminated.append("other-local-work")
    if usage["messages"] == 0:
        contaminated.append("no-tokens-in-the-logs")

    # The percent belongs to the account, so the rate uses all local work.
    rate = {}
    if "account-switch" not in contaminated:
        for name, key in (("fiveHour", "five-hour"), ("sevenDay", "weekly")):
            window = track.get(name)
            if window and window["delta"] >= 1 and f"{key}-reset" not in contaminated and all_weighted > 0:
                rate[name] = round(all_weighted / window["delta"])
    result = {
        "ok": True,
        "measureId": wanted,
        "label": start.get("label"),
        "startedAt": _iso(began),
        "endedAt": _iso(now),
        "durationSeconds": round(now - began),
        "usage": usage,
        "otherLocalWeightedTokens": round(others),
        "percent": track,
        "weightedTokensPerPoint": rate or None,
        "contaminated": contaminated,
        "unit": token_model.UNIT,
        "assumptions": [
            "percent readings are whole numbers from the engine's polls and can be minutes old",
            "use of the account outside this machine is not visible",
        ],
    }
    _append(path, {"schemaVersion": SCHEMA_VERSION, "event": "stop", "source": "passive",
                   "recordedAt": _iso(now), **{k: v for k, v in result.items() if k != "ok"}})
    return result


# --- Tasks ----------------------------------------------------------------


def record_task(backup_root: Path, projects_dir: Path, fields: dict, now: float) -> dict:
    """Store one finished task of an orchestrator, with its tokens from the logs.

    ``agent_id`` (preferred) or ``session_id`` is the key into the logs.
    ``started_at`` and ``ended_at`` are ISO 8601 times with a zone.
    """
    fields = fields if isinstance(fields, dict) else {}
    began, ended = usage_log.parse_ts(fields.get("started_at")), usage_log.parse_ts(fields.get("ended_at"))
    if began is None or ended is None or not 0 <= ended - began <= MAX_MEASURE_S:
        return {"ok": False, "error": "bad-times"}
    role, tier = clean(fields.get("role"), 32), clean(fields.get("tier"), 32)
    if not role:
        return {"ok": False, "error": "role-required"}
    project = fields.get("project_hash")
    if project is not None and not (isinstance(project, str) and _HASH.match(project)):
        return {"ok": False, "error": "project-hash-must-be-a-hash"}
    outcome = fields.get("outcome") if fields.get("outcome") in OUTCOMES else "done"
    agents, sessions = _ids(fields.get("agent_id")), _ids(fields.get("session_id"))
    attempt = fields.get("attempt")

    usage = None
    if agents or sessions:
        messages = usage_log.read_messages(projects_dir, began - 1.0, ended + 1.0)
        usage = usage_log.summarize(usage_log.select(messages, sessions, agents))
    models = sorted(usage["models"]) if usage else []
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "source": "passive",
        "recordedAt": _iso(now),
        "taskKey": uuid.uuid4().hex[:12],
        "taskId": clean(fields.get("task_id")),
        "address": clean(fields.get("address")),
        "role": role.lower(),
        "tier": tier.lower() if tier else None,
        "model": clean(fields.get("model"), 64),
        "tool": clean(fields.get("tool"), 32),
        "projectHash": project,
        "attempt": attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None,
        "agentIds": agents,
        "sessionIds": sessions,
        "startedAt": _iso(began),
        "endedAt": _iso(ended),
        "durationSeconds": round(ended - began),
        "outcome": outcome,
        "usage": usage,
        "modelsSeen": models,
    }
    _append(Path(backup_root) / TASKS_FILENAME, row)
    matched = bool(usage and usage["messages"])
    return {
        "ok": True,
        "taskKey": row["taskKey"],
        "matchedInLogs": matched,
        "weightedTokens": usage["weightedTokens"] if matched else None,
        "modelsSeen": models,
        "note": None if matched else "no token found for these ids in the local logs: the task is stored without usage",
    }


def _norm(value) -> str | None:
    text = clean(value, 64)
    return text.lower() if text else None


def estimate_task(backup_root: Path, role, tier=None, model=None, project_hash=None) -> dict:
    """Cost of past tasks like this one: median, range and n. No guess.

    The match starts exact and widens until it has ``MIN_N_FOR_ESTIMATE``
    tasks: role+tier+model+project, role+tier+model, role+tier, role. Only
    finished tasks with tokens count. Below that number the answer is
    ``unknown``.
    """
    role, tier, model = _norm(role), _norm(tier), _norm(model)
    rows = [
        r for r in _read(Path(backup_root) / TASKS_FILENAME)
        if r.get("outcome") == "done" and isinstance(r.get("usage"), dict) and r["usage"].get("weightedTokens")
    ]

    def keep(r, use_tier, use_model, use_project) -> bool:
        if r.get("role") != role:
            return False
        if use_tier and tier and r.get("tier") != tier:
            return False
        if use_model and model and _norm(r.get("model")) != model:
            return False
        return not (use_project and project_hash and r.get("projectHash") != project_hash)

    levels = ((True, True, True), (True, True, False), (True, False, False), (False, False, False))

    def name_of(use_tier, use_model, use_project) -> str:
        parts = ["role"]
        parts += ["tier"] if use_tier and tier else []
        parts += ["model"] if use_model and model else []
        parts += ["project"] if use_project and project_hash else []
        return "+".join(parts)

    best_name, best = "role", []
    for flags in levels:
        matched = [r for r in rows if keep(r, *flags)]
        if len(matched) > len(best) or (not best and not matched):
            best_name, best = name_of(*flags), matched
        if len(matched) >= MIN_N_FOR_ESTIMATE:
            best_name, best = name_of(*flags), matched
            break
    numbers = stats([r["usage"]["weightedTokens"] for r in best])
    word = sample_confidence(numbers["n"])
    out = {
        "role": role, "tier": tier, "model": model,
        "matchedOn": best_name,
        "confidence": word,
        "unit": token_model.UNIT,
        "weightedTokens": numbers,
        "durationSeconds": stats([r.get("durationSeconds") or 0 for r in best]),
        "assumptions": ["only finished tasks with tokens in the local logs count"],
    }
    if word == "unknown":
        out["note"] = (
            f"fewer than {MIN_N_FOR_ESTIMATE} recorded tasks match: no estimate. "
            "Record tasks with record_task to build the history."
        )
    else:
        median = numbers["median"]
        out["percentOfWeek"] = {
            plan: round(100.0 * median / (100.0 * token_model.prior_rate(plan, "week")), 2)
            for plan in ("pro", "max")
        }
        out["assumptions"].append("percentOfWeek uses the community prior rate of each plan")
    return out


def fits(backup_root: Path, tasks, budget_for_hours, hours: float) -> dict:
    """Do these tasks fit in the fuel reachable within ``hours``?

    ``tasks`` is a list of ``{"role", "tier", "model", "project_hash",
    "count"}``. ``budget_for_hours(hours)`` returns a budget whose first
    horizon is ``hours``. A task with no estimate makes the answer unknown.
    """
    rows, median_total, worst_total, unknown = [], 0.0, 0.0, []
    for task in (tasks if isinstance(tasks, list) else [])[:200]:
        if not isinstance(task, dict):
            continue
        count = task.get("count", 1)
        count = count if isinstance(count, int) and not isinstance(count, bool) and 0 < count <= 1000 else 1
        estimate = estimate_task(backup_root, task.get("role"), task.get("tier"), task.get("model"),
                                 task.get("project_hash"))
        numbers = estimate["weightedTokens"]
        row = {"role": estimate["role"], "tier": estimate["tier"], "model": estimate["model"],
               "count": count, "confidence": estimate["confidence"], "n": numbers["n"],
               "medianEach": numbers["median"], "p75Each": numbers["p75"]}
        rows.append(row)
        if estimate["confidence"] == "unknown":
            unknown.append(row)
        else:
            median_total += count * numbers["median"]
            worst_total += count * numbers["p75"]
    budget = budget_for_hours(hours)
    horizon = (budget.get("horizons") or [{}])[0]
    reachable = horizon.get("reachableTokens")
    answer = "unknown"
    if not unknown and rows and isinstance(reachable, (int, float)):
        answer = "yes" if worst_total <= reachable else ("tight" if median_total <= reachable else "no")
    return {
        "fits": answer,
        "hours": hours,
        "reachableTokens": reachable,
        "neededTokens": {"median": round(median_total), "p75": round(worst_total)},
        "tasks": rows,
        "unknownTasks": len(unknown),
        "advice": budget.get("advice"),
        "tokenConfidence": (budget.get("tokens") or {}).get("confidence"),
        "unit": token_model.UNIT,
        "assumptions": [
            "yes = the p75 total fits; tight = only the median total fits; unknown = a task has no history",
            "reachableTokens is an estimate: see tokenConfidence",
            "the budget only limits planned work; it is never a reason to create work",
        ],
    }
