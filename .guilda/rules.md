# Project rules

## What must never happen

### P1 — No identity or secrets in guilda output
Rule:  Never put an email, org name, account name, token or credential in pace output; plan type is allowed.
Why:   guilda output reaches logs and agents that must not hold personal data.
Check: gate — The Security role and a test that scans pace output, also on errors.

### P2 — Read-only pace, no guessed values
Rule:  Keep pace read-only, and report unreadable usage as unavailable, never as a guessed value.
Why:   A guessed value makes guilda pace its agents on false data.
Check: gate — The Reviewer rejects a switch call or an invented value in pace code.

## Domain rules

### P3 — Stable JSON contract
Rule:  Raise schemaVersion only on a breaking change, and write reset times in ISO 8601 with a time zone.
Why:   Scripts and guilda read the output and depend on it.
Check: gate — The Reviewer checks each payload change against schemaVersion.

## Project adjustments

### P4 — Fork git flow
Rule:  Work on a feature branch and open pull requests to origin, never pushing to main or upstream.
Why:   upstream belongs to the original author; main must stay clean.
Check: judgment — No hook blocks a push; the owner checks the remote before a push.
