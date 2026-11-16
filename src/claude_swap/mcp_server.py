"""``cswap mcp``: a local, read-only MCP server (stdio) for planning.

Tools:

- ``get_budget``: the whole fleet budget (see ``budget.build_budget``).
- ``get_advice``: only the advice word, its reason and the fleet sums.
- ``measure_account``: token rate of an account and its confidence.
- ``measure_start`` / ``measure_stop``: tokens and percent of a stretch of work.
- ``record_task``: one finished task of an orchestrator, tokens from the logs.
- ``estimate_task``: cost of past tasks like this one (median, range, n).
- ``fits``: do these tasks fit in the fuel reachable within some hours.

The budget tools only read. The measurement tools append to local files
(``tasks.jsonl``, ``measures.jsonl``) and change no account.

The server holds what ``cswap pace`` holds: account numbers and plan words.
No email, no organization or account name, no token. It makes no network
call and changes no account. It needs the ``mcp`` package:
``pip install 'claude-swap[mcp]'``.
"""

from __future__ import annotations

import sys

INSTRUCTIONS = (
    "cswap manages several Claude accounts on this machine. Use get_budget to plan work: "
    "it gives the quota left on each account (percent and capacity units), when each "
    "window resets, how many accounts are usable now, and an advice word: can_speed_up "
    "(quota will expire unused), ok, slow_down (the fleet ends before its resets) or "
    "blocked (no account usable now). Use it to decide how many tasks fit now and which "
    "ones wait for a reset. Figures are estimates: plan sizes are not published. "
    "Do not create work only to spend quota. To build cost history: the controller of a "
    "task calls record_task when it ends, or measure_start and measure_stop around a "
    "piece of work; then estimate_task and fits answer planning questions. Every figure "
    "carries a confidence word; `unknown` means there is no data yet, not zero."
)


def _server_class():
    """The high-level server class of the installed MCP SDK, or None.

    SDK 2.x names it ``MCPServer``; SDK 1.x named it ``FastMCP``.
    """
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError:
        return None


def run() -> int:
    """Serve on stdio until the client closes; 1 if the MCP package is missing."""
    server_class = _server_class()
    if server_class is None:
        print("cswap mcp needs the MCP package: pip install 'claude-swap[mcp]'", file=sys.stderr)
        return 1
    from claude_swap.budget_source import current_budget

    server = server_class("cswap", instructions=INSTRUCTIONS)

    @server.tool()
    def get_budget() -> dict:
        """Quota budget of the Claude account fleet, for planning tasks.

        Returns the advice (can_speed_up, ok, slow_down, blocked, unknown) with its
        reason; fleet sums (capacity units, remaining units, usable accounts, pace
        ratio, expected loss); each account (number, plan, 5-hour and weekly percent,
        reset times, remaining units, usable now or when); the list of weekly resets
        with the units that expire at each; and a token estimate when one was measured.
        1 capacity unit = the weekly quota of one Pro account. Read-only, local.
        """
        return current_budget()

    @server.tool()
    def get_advice() -> dict:
        """Short pacing advice: the advice word, its reason and the fleet sums."""
        full = current_budget()
        return {key: full[key] for key in ("generatedAt", "advice", "reason", "fleet")}

    import time

    from claude_swap import budget_source, measure, paths, usage_log

    def root():
        return paths.get_backup_root()

    @server.tool()
    def measure_account(account: int | None = None) -> dict:
        """Token rate of one account or of all: weighted tokens per percent point of the
        weekly and 5-hour windows, the community prior, the account's own points and a
        confidence word (prior, low, medium, high)."""
        return budget_source.measure_account(account)

    @server.tool()
    def measure_start(label: str | None = None, session_ids: list[str] | None = None,
                      agent_ids: list[str] | None = None) -> dict:
        """Open a measurement around a piece of work. Call it from the controller of the
        work, not from the agent that does the work. With session_ids or agent_ids only
        the tokens of those ids count; with none, all local work counts. Returns measureId."""
        return measure.measure_start(root(), time.time(), label, session_ids, agent_ids)

    @server.tool()
    def measure_stop(measure_id: str) -> dict:
        """Close a measurement: tokens by type and weighted tokens from the local logs,
        models seen, duration, the percent of the active account at the start and the end,
        weighted tokens per percent point when the percent moved, and `contaminated`: the
        reasons why the measure may be wrong (account switch, reset, other local work,
        percent within its resolution, no percent readings)."""
        return measure.measure_stop(root(), usage_log.claude_projects_dir(), measure_id, time.time())

    @server.tool()
    def record_task(role: str, started_at: str, ended_at: str, tier: str | None = None,
                    model: str | None = None, tool: str | None = None, task_id: str | None = None,
                    address: str | None = None, project_hash: str | None = None,
                    attempt: int | None = None, agent_id: str | None = None,
                    session_id: str | None = None, outcome: str = "done") -> dict:
        """Record one finished task of an orchestrator. cswap sums its tokens from the local
        logs by agent_id (preferred) or session_id; the caller counts nothing. Times are
        ISO 8601 with a zone; with an agent_id they may be approximate (the answer gives
        firstTokenAt and lastTokenAt), with a session_id only they bound what counts. project_hash must be a hash, never a path. outcome is one of
        done / finished (stored as done), failed / error (stored as failed), stalled /
        limit / timeout (stored as stalled); another word is refused. Only tasks stored as
        done feed estimate_task and fits. Recording the same agent_id (or task_id and
        attempt) again replaces the earlier record, so a task with matchedInLogs false
        can be recorded again later."""
        return measure.record_task(root(), usage_log.claude_projects_dir(), {
            "role": role, "started_at": started_at, "ended_at": ended_at, "tier": tier,
            "model": model, "tool": tool, "task_id": task_id, "address": address,
            "project_hash": project_hash, "attempt": attempt, "agent_id": agent_id,
            "session_id": session_id, "outcome": outcome,
        }, time.time())

    @server.tool()
    def estimate_task(role: str, tier: str | None = None, model: str | None = None,
                      project_hash: str | None = None) -> dict:
        """Cost of past tasks like this one, in weighted tokens: median, mean, p25, p75 and
        n, with a confidence word. With fewer than 5 recorded tasks the answer is `unknown`:
        there is no guess."""
        return measure.estimate_task(root(), role, tier, model, project_hash)

    @server.tool()
    def fits(tasks: list[dict], hours: float = 24.0) -> dict:
        """Do these tasks fit in the fuel reachable within `hours`? tasks is a list of
        {role, tier, model, project_hash, count}. Answer: yes (the p75 total fits), tight
        (only the median total fits), no, or unknown (a task has no history)."""
        hours = max(1.0, min(float(hours), 24.0 * 14))
        return measure.fits(
            root(), tasks,
            lambda h: budget_source.current_budget(horizon_hours=(h,)), hours,
        )

    server.run()
    return 0
