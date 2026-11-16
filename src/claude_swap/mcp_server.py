"""``cswap mcp``: a local, read-only MCP server (stdio) for planning.

Tools:

- ``get_budget``: the whole fleet budget (see ``budget.build_budget``).
- ``get_advice``: only the advice word, its reason and the fleet sums.

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
    "Do not create work only to spend quota."
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

    server.run()
    return 0
