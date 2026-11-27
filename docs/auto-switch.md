# Automatic switching: the exact rules

Reference for `cswap auto`, and for the menu bar's auto-switch, which runs the same engine. Everyday use is in the [README](../README.md#automatic-switching).

## Strategies

Set with `--strategy`, or `cswap config set autoswitch.strategy`.

- `best` (default) stays put until the active account nears its limit, then moves to the account with the most quota left.
- `weekly-first` switches at the same lines as `best`, with the same landing rules and hysteresis margin, but among the accounts that qualify it lands on the one whose **weekly window resets soonest** (most quota left breaks ties), so weekly quota that would otherwise expire unused gets spent first. Its at-limit and failover escapes, and the all-over-the-lines case, rank by quota left exactly as `best` does.
- `consume-first` proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- `priority-consume-first` follows your own ranking first (see *Account priority tiers* below), then the soonest weekly reset within a tier.

## Work days

Set with `--work-days mon-fri`, or `cswap config set autoswitch.workDays mon-fri`. The flag replaces the setting for one session, as `--threshold` does.

The `balanced` strategy counts only work time: the seconds that fall on a work day of the local calendar (the time zone of the machine). Time on the other days is rest time.

- **Forms.** `all`, a range (`mon-fri`), a list (`mon,wed,fri`), or a mix (`mon-wed,fri`). The day words are `mon tue wed thu fri sat sun`. Case is ignored. A range does not wrap the week, so `fri-mon` is refused.
- **Normal form.** The setting stores and shows `all` for 7 days. Otherwise it lists the days in the order Monday to Sunday. A run of 3 or more consecutive days is written `a-b`; other days are single words, joined by `,`. Examples: `mon-fri`, `mon-sat`, `mon,wed,fri`, `mon-wed,fri`.
- **Default.** `all`. With `all`, the engine keeps the wall-clock figures of earlier versions.
- **Bad values.** `cswap config set` refuses a bad value with exit code 1. A bad stored value reads as `all`. A value with no day reads as `all`.
- **Work share.** The work share of an account is the share of the work time of its week that has passed. The week of an account is the 168 hours before its next weekly reset. The balanced strategy uses the work share in place of the wall-clock share. When the remaining work time is 0 and the reset is still ahead, the work share is 1.
- **Rest-time use.** Use on a rest day counts as usual. Each tick recomputes the work share, the pace, the needs and the deficits, so the engine recalculates when you work on a rest day.

## Loss guard (balanced)

The loss guard puts first the usable account whose quota expires before the engine can spend it in work time. It works at any usage level.

1. The fleet pace is the capacity-weighted used share divided by the capacity-weighted work share, over the measured accounts that count.
2. The fleet work rate is `max(pace, RATE_FLOOR) * capacity of the fleet / work hours of one full week`, in capacity units per work hour. `RATE_FLOOR` is 0.5. A full week has 120 work hours for `mon-fri` and 168 for `all`.
3. The need of an account is its capacity times the weekly quota it still has to spend to reach its weekly line, divided by the fleet work rate. The need is in work hours.
4. An account is a guard account when its remaining work time is at most `GUARD_MARGIN` times its need. `GUARD_MARGIN` is 1.25.
5. An account whose remaining work time is 0 is a guard account when it has any need. So use on a rest day goes first to the quota that expires before the next work day.

Guard accounts rank first, the earliest next weekly reset first. The other accounts follow by deficit. A guard candidate moves the active account when the active account is not a guard account. A guard active account stays, unless a guard candidate has an earlier next reset. Then the engine moves to that candidate. The cooldowns apply as for any other balanced switch.

## Cooldown and hysteresis

A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is already over its switch lines, it moves to the one with the most quota left before a limit (the lowest usage on its fullest window, whatever line that window carries) under `best`, `weekly-first` and `consume-first` alike, once that account has twice the current one's remaining quota or 3 points more than it, whichever is less. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset. `priority-consume-first` does neither: it holds rather than land on an account over its lines (see [Account priority tiers](#account-priority-tiers)).

Under `consume-first` the margin (10 points by default, `autoswitch.hysteresisPct`) is instead the room a landing needs. A move made below the threshold only lands on an account with at least that many points left under every one of its lines, each window against its own line: the cooldown that move starts would otherwise hold it on an account that crosses its line minutes later. Setting the margin to 0 removes that bar. A move forced by the active account's line prefers targets with that room, under `weekly-first` too, and still takes the soonest reset when none has it. An at-limit or failover escape lands on an account below its lines before one over them under every strategy; under `consume-first` it keeps the soonest-reset order only among accounts with that room, and otherwise takes the one with the most quota left.

## A separate line for the weekly windows

Set with `--weekly-threshold 97`, or `cswap config set autoswitch.weeklyThreshold 97`.

One threshold compared against the busiest window forces the same number on windows that want different ones — the 5-hour window recycles in hours (an overshoot costs one interrupted turn), while the weekly window is the budget (quota unspent before its reset is gone). With a weekly line, the 7-day window and any per-model window (`--model`) are judged against it and the threshold keeps governing the 5-hour window; every window is compared against its own line, so 7d 93% under a 97 line holds while 5h 95% under an 85 line switches, and the log names the window it measured (`7d 93% < 97%`). Points on different windows are not the same amount of work — the lines are comparable, the clocks are not.

## Per-account threshold

`cswap threshold <num|email> <pct>` gives one account its own switch-away line (50–99.9) in place of the global one: auto-switch leaves that account when *its* line is reached and prefers candidates still under *theirs*, with ranking and the hysteresis margin counting room under each account's own line rather than raw headroom. Like the global threshold it is a switch-away line, not a hard cap — with nothing under its line, or the active account at its limit, it still lands somewhere rather than stall (`priority-consume-first` is the exception: it holds, see below). With a weekly line set, the override applies to the 5-hour window only. Accounts without one keep the global threshold. `cswap threshold` alone lists the default and every override, `--unset` clears one, and the [TUI](../README.md#interactive-dashboard-tui) shows a `th 85%` badge.

## Account priority tiers

`cswap priority <num|email> <tier>` ranks an account for the `priority-consume-first` strategy: higher tiers are used first, accounts without one are tier 0 (valid tiers are 0–1000), and within a tier the account whose weekly window resets soonest goes first. It moves back to a higher tier as soon as that account is usable again (after the cooldown), and it never lands on an account at or over its switch lines — when nothing qualifies, even at the limit, it holds and keeps polling instead of relaxing them. Tiers alone change nothing; `best`, `weekly-first`, `consume-first`, and manual switching ignore them. It uses neither the hysteresis margin nor the exhausted slow cadence described above: equal tiers and reset times keep the active account, and a blocked state keeps polling at the normal interval. `cswap priority` lists every account's tier and `--unset` clears one.

## Holding an account out of rotation

To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `cswap disable <num|email>`; `cswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `cswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cswap switch <num|email>` target. They show a `(disabled)` marker in `cswap list`, in the [TUI](../README.md#interactive-dashboard-tui), and in the [menu bar](../README.md#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).

## Per-model limits

By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `cswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.

**Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively. The exact strings for your accounts are the per-model rows in `cswap list` (e.g. a line reading `Fable: 100%`).

A setup-token account (`cswap add-token`) has a Fable row only with `usage.fableProbe` on, next to `usage.headerProbe`. Without it, `--model Fable` leaves that account's reading shown but not used for switching.

## Accounts with no 5-hour window

When the usage API reports no `five_hour` window for an account (verified Ultra accounts have no 5-hour limit), cswap judges it on its 7-day window and any `--model` window alone and shows no 5h row. A setup-token read through the rate-limit headers (`usage.headerProbe`) is judged the same way when its response carries no 5-hour headers at all; a 5-hour header without a usable utilization fails the reading instead.

## Usage polling

Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.

## When something fails

It fails safe: if a usage check errors it keeps trusting the last-known numbers, for a bounded time, while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).

An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `cswap add --slot N`, or replace its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.

## Limit-hit watcher

Usage readings are stored between fetches, so the engine can decide on old numbers while agents fill a window. The limit-hit watcher closes that gap with the error that Claude Code writes in its own transcripts.

- Every 5 seconds (`LIMIT_WATCH_S`) it reads the new lines of the transcript files under `~/.claude/projects/` (any depth, subagent transcripts included). It reads only the error fields and the usage fields of a line. It logs no text and no path.
- A limit hit is an assistant line with `isApiErrorMessage` true and `apiError` `usage_limit_reached`, or `error` `rate_limit` with "limit" in the text. The text names the window: "5-hour" is the 5-hour window, "weekly" or "week" is the 7-day window, other text is the window with the highest percent in the last reading.
- At the first start it begins at the end of each file. It does not replay old hits, and it keeps its offsets in memory only.
- A hit counts for the active account only when its timestamp is 10 seconds (`LIMIT_SLACK_S`) or more after the last switch to that account. An older hit belongs to the previous account.

### Forced switch on a hit

A counted hit records that window of the active account as 100% used until its `resetsAt` (5 hours or 7 days from the hit when no reset is known). The engine then runs a tick at once with the trigger `limit-hit`. The switch has no cooldown and no hysteresis margin, as at the threshold. The next tick asks for one fresh reading of the old account, inside the urgent-mode budget. When no account is usable, the all-exhausted handling applies.

An agent that already stopped on the error does not restart. The switch only sends later requests to a usable account.

### Estimate between readings

Between two readings the engine estimates the active account's 5-hour percent: the last percent plus the weighted tokens since the fetch time, divided by the `tokensPerPercent` of that window. It uses the estimate only when the confidence of that calibration is `medium` or `high`.

1. When the estimate is within the escalation margin of the line, the engine starts urgent mode (one fresh reading at once, inside its budget).
2. When the estimate reaches the line, the engine switches with the trigger `estimate`.

The budget and the menu bar pill show the stored reading, not the estimate.

## Running alongside Claude Code

Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.

## Cron and systemd timers

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

## Defaults

Defaults like the threshold, the cooldown and the work days are configurable with `cswap config set autoswitch.threshold 80` — flags override them (see [Configuration](../README.md#configuration)).
