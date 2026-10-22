# An opt-in Messages API planner under a named service key

- decided: 2026-10-20
- keeps: GOV-17 (no paid API fallback, credits, purchases
  or usage-limit evasion)
- paths: operating/messages_planner.py
- status: decided

## Question

The model API budget rule said "no paid API fallback; batch extraction and eval runs use Claude Code sessions". The
owner orders a Messages API backend for the agent loop's planner. May
the Workbench call the Messages API, and on what terms?

## Decision

1. An opt-in Messages API planner is allowed (`operating/messages_planner.py`,
   `MessagesPlanner`). It is selected only when `PLX_ASK_PLANNER` is `messages`. Claude Code
   (`ClaudeCodePlanner`) stays the default.
2. Claude Code is also the fallback. A Messages planner that cannot be built (no key, no
   model setting) falls back to Claude Code and reports the refusal beside it.
3. No automatic paid fallback. Nothing switches to the Messages API on its own: not a Claude
   Code failure, not a usage limit, not a timeout. GOV-17 stands; this record does not widen it.
4. The key is a named service key from the planner's named settings (`KEY_SETTINGS`:
   `ANTHROPIC_API_KEY`, else `PLX_ANTHROPIC_API_KEY`). Only the credentialed environment
   holds it. It is never printed, logged, put in an error, a `repr`, a report or a commit.
   A live check that runs the planner declares the setting in `requires_env`.
5. The one HTTP sender is `post_messages`: one POST to `https://api.anthropic.com/v1/messages`
   only (any other url raises before a request), redirects refused. It reaches no business
   system. `scripts/check_http_egress.py` allowlists exactly that function and pins the file
   to that URL: another network call in the file, even a GET, or another URL string fails the
   check (`outside-sender`, `unlisted-url`).
6. The request carries the tool results this employee's session returned, after its
   permission trimming. They leave the host for the model provider, as Claude Code's do.
7. Cost is measured, never estimated: token counts per request, and money only from the
   `PLX_MESSAGES_PRICES` setting as Decimal (no built-in price).

## Why

- The owner ordered the backend. The rule forbade a paid *fallback*; an explicit
  opt-in under the owner's own key is not a fallback.
- Keeping Claude Code as default and fallback means no run starts paying without a setting a
  person set.

## Evidence

- Before: the egress scan (1185f200) flags `operating/messages_planner.py`
  4 times (urllib-import x3, non-get-request in `post_messages`). After: clean, with
  `post_messages` excused (`tests/test_no_write_http_paths.py`,
  `test_messages_planner_post_is_its_one_sender_and_url`).
- Live check `verify/live-checks/gate.http_egress.json` reports the allowlisted senders the
  scan excused and zero unlisted senders. Live-unverified until the verifier records it. The
  planner itself is live-unverified until its own live check has a result.

## Reversal

Reverse if the owner withdraws that order or sets a budget the planner cannot meter. Then
drop the allowlist entry (`ALLOWED`, `URLLIB_FILES`, `SENDER_URLS`) and the planner selection.
