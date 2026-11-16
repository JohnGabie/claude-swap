"""Priority strategies do not get the forced no-return bar (E3): upstream
allows a failover back to the account just left."""
from claude_swap.autoswitch import TickOutcome
from tests.test_priority_strategy import fleet, usage


def test_priority_failover_returns_to_account_just_left(temp_home):
    h = fleet(temp_home, unhealthy_ticks=1)
    values = usage(h, pcts=(100, 10, 100, 100))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED   # 1 -> 2, at-limit
    assert h.active_number() == 2
    h.clock.advance(60)
    values = usage(h, pcts=(0, 10, 100, 100))
    values["2"] = None                                         # 2 unreadable
    outcome = h.tick_with_usage(values)
    reasons = [getattr(e, "reason", None) for e in h.events]
    assert outcome is TickOutcome.SWITCHED, reasons
    assert h.active_number() == 1
