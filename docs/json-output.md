# JSON output reference

`cswap list --json`, `cswap status --json` and `cswap switch --json` print a single JSON object on stdout; human-readable notices go to stderr. The commands and an example are in the [README](../README.md#json-output-for-scripting).

## Contract

- Every payload carries a `schemaVersion` (currently `1`).
- On a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code.
- `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

## Usage windows

Each account row has a `usage` object with `fiveHour` and `sevenDay` windows (`pct`, `resetsAt`).

- A window's `pct` is `null` when the usage API reported `utilization: null` for it.
- A window the account does not have (an Ultra account's `fiveHour`) is omitted.
- A per-model `scoped` window carries `probeAfter` (ISO-8601) when a setup-token's Fable probe (`usage.fableProbe`) found it used up: the row is kept as read, and Fable is not asked again before that time.
- Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`).

## Optional row fields

These apply to `list` rows and the managed active row from `status --json`. All are additive.

| Field | Present when | Meaning |
|---|---|---|
| `usageFetchedAt`, `usageAgeSeconds` | the row has decision-trusted usage | How old the measurement is. |
| `lastGoodUsage`, `lastGoodFetchedAt`, `lastGoodAgeSeconds` | `usage` is null but a last-known measurement exists: data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` | Preserves the human display without making the account actionable. |
| `usageError` | `usage` is null and nothing else explains it (`usageStatus` is `unavailable`) | The last fetch failure by kind (e.g. `http-429`, `timeout`). |
| `usageRetryAt` | the cache is backing off from that failure | The time of the next attempt. |
| `disabled` | the account is held out of rotation with `cswap disable` | `true`; absent otherwise. |
| `threshold` | a per-account threshold is set with `cswap threshold` | e.g. `85.0`; absent otherwise, so a fleet that never sets a policy emits rows identical to before. |
| `alias` | an alias is set with `cswap alias` | e.g. `"dev"`; accounts without one omit the key. |
| `loginExpiresAt` | the stored login records when its refresh token expires | ISO-8601 UTC: the moment the slot will need a fresh `/login` and `cswap add --slot N`, so a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login. |
| `plan` | the slot's subscription plan has been read from the profile endpoint at least once | `label` as the human views print it (`Ultra`, `Max 5x`, `Pro`, `Team`, `Free`), the raw `tier`, then `organizationType`, `billingType`, `subscriptionStatus` and `planDisplayName` when the profile carried them, `expired` (`true` once the subscription is `canceled` and the account is back on the free tier; the human views show that as a highlighted `Free`) and `fetchedAt`, the last successful profile read. Plans refresh on their own six-hour cadence, not with usage. |

## Pace fields on weekly windows

Weekly windows (`sevenDay` and per-model `scoped` entries, never `fiveHour`) additively carry pace fields once the week is about a day old.

| Field | Meaning |
|---|---|
| `expectedPct` | Where usage would sit if spread evenly across the week. |
| `aheadOfPace` | `true` when meaningfully above that: the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker. |
| `projectedExhaustionAt` | The current rate extrapolated into an ETA to 100%. |
| `willLastToReset` | Whether the quota will last to the reset at the current rate. |

The two projections stay `--json`-only, since a linear projection is too rough to present as fact in the UI.

## Budget (CLI budget command, MCP `get_budget`)

The budget payload carries `schemaVersion` `1`. The pace fields count work time, so a rest day does not move them.

| Field | Meaning |
|---|---|
| `fleet.workDays` | The work days in normal form (`all`, `mon-fri`, `mon,wed,fri`, ...). Added; absent from older payloads. |
| `accounts[].sevenDay.elapsedPct` | The work share of the account week, in percent: the share of the work time of its week that has passed. With `all` it equals the wall-clock share. |
| `accounts[].sevenDay.projectedEndPct` | `min(100, pct / work share)`, with the work share unrounded. |
| `fleet.paceRatio` | Used share divided by work share over the fleet, weighted by capacity. |
| `advice` | `can_speed_up` when `paceRatio` is below `SPEED_UP_RATIO` (0.95). `slow_down` when it is above `SLOW_DOWN_RATIO` (1.05). `ok` between the two. |

The types of the fields do not change. Only their meaning becomes "of the work time", so `schemaVersion` stays `1`. The token horizons (`reachableTokens`) keep wall-clock time.

## `cswap auto --json`

`cswap auto --json` emits an event stream instead: one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}`) with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`. The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### The `limit-hit` event

When the limit-hit watcher counts a hit (see [auto-switch.md](auto-switch.md#limit-hit-watcher)), the decision log and the event stream get one event before the switch event:

```json
{"schemaVersion": 1, "event": "limit-hit", "ts": "2026-11-27T22:21:22Z", "account": 4, "window": "5h"}
```

`account` is the number of the active account. `window` is `"5h"` or `"7d"`. The event holds no transcript text and no path.

A hit on an active account whose reading is unknown or distrusted makes that account at its limit, and the engine switches at once.

### Triggers `limit-hit` and `estimate`

The `trigger` of a `switch` event can be `limit-hit` (Claude Code reported the limit of the active account) or `estimate` (the token estimate reached the switch line). Scripts must accept unknown trigger values.
