# Bot Operations

## Live startup fixes verified on 2026-10-24

The official web client and authenticated read-only probes confirmed:
`GET /positions?include_zero=true` includes cumulative `realized_pnl` and
`unrealized_pnl`, including closed positions (flat rows have null unrealized PnL).
`GET /orders?limit=500` exposes account order IDs, quantities, fill quantities and
terminal status. `GET /system/bot-trading` exposes bot availability. These routes
were found in the official web client, not inferred from the public trade tape.

Set `CTC_ACCOUNTING_PROVIDER=ctc.execution.accounting:read_snapshot` to use these
verified account fields. This provider tracks the change in authoritative cumulative
account PnL from a persisted session baseline; it does not invent starting equity or
fill prices. Order states are reconciled by acknowledged ID; omitted bot IDs, invalid
fields, stale snapshots and ambiguous submissions continue to block live trading.

For an explicitly shared manual account, `CTC_PRESERVE_EXTERNAL_ORDERS=true` preserves
orders outside this bot's ACK registry, includes all account positions/open orders in
risk calculations, and never cancels external orders. Default remains false. Earlier
unresolved external IDs require explicit operator reconciliation before restarting.
Account PnL and the session loss stop include manual trades as well as bot trades.

Inherited holdings over the initial gross cap may be reduced, but increases remain
blocked. The PORT one-share passive test chooses a sell above the ask for an inherited
long, otherwise a buy below the bid. Both sides require ACK, matching open-order readback,
confirmed cancellation, disappearance and unchanged position. Initial limits remain
one share/order, five actions/minute and $10,000 gross; dry-run remains the default.

Quiet quotes are refreshed from actual REST books rather than assuming every symbol
receives a heartbeat. Initial snapshots restore prior final damage reports. Unsent
observation proposals do not consume churn prices. Strategy orders must have positive
executable edge; a flat target alone is not a profitable liquidation signal. The rate
limiter no longer recursively acquires its own lock, and budget exhaustion keeps the
feed loop running instead of blocking it.

To stop an instance cooperatively, create a file beside its lock with suffix `.stop`
(e.g. `research/runtime/bot.stop`). The loop exits and cancels only its own tracked orders.
Remove that stop file deliberately before restarting. Do not delete a held lock.

Validation: 197 tests with the research dataset, Ruff lint/format, authenticated account
reconciliation, all-symbol market feed, actual passive PORT place/cancel with zero fills,
and exchange-confirmed strategy fills. No credentials or account recordings are committed.

How to install, configure, and run the CTC 2026 trading bot and its tooling. The bot
**defaults to dry-run** and will not submit any state-changing request unless explicitly
switched to live.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,live]"     # dev = tests/lint, live = websockets
```

## Configure (`.env`)

Create `.env` at the repo root (already gitignored). Only `CTC_API_KEY` is required.

```ini
CTC_API_KEY=your_bot_key_here      # secret; never commit
# --- optional overrides (defaults shown) ---
# CTC_DRY_RUN=true                 # MUST be false (and --live) to submit orders
# CTC_KILL_SWITCH=false
# CTC_HTTP_URL=https://cornelltradingcompetition.org
# CTC_MAX_ORDER_SIZE_FRAC=0.25     # per-order cap as fraction of the symbol position limit
# CTC_MAX_GROSS_EXPOSURE=2000000   # max |notional| across the book ($)
# CTC_MAX_ORDERS_PER_MIN=50        # below the 60/bot exchange ceiling
# CTC_MAX_SESSION_LOSS=10000       # session stop, using verified realized + unrealized PnL
# CTC_INITIAL_MAX_GROSS_EXPOSURE=10000
# CTC_INITIAL_MAX_ORDER_SIZE_FRAC=0.01
# CTC_INITIAL_MAX_ORDER_UNITS=1
# CTC_INITIAL_ORDERS_PER_MIN=5
# CTC_AUTO_TRANSITION=false        # leave false until all live checks are independently verified
# CTC_MIN_REPRICE_EDGE=0.02        # churn control: min price move to re-quote ($)
# CTC_STALE_AFTER_S=10             # don't trade on quotes older than this
```

Verify config is loaded and the key is masked:

```bash
python -c "from ctc.execution.settings import LiveConfig; import json; \
print(json.dumps(LiveConfig.from_env().redacted(), indent=2))"
```

## CLIs

| Command | What it does | State-changing? |
|---------|--------------|-----------------|
| `ctc-bot-probe [--listen N]` | Read-only connectivity check (public + auth; optional WS listen) | No |
| `ctc-bot-replay PATH [--step-every N] [--verbose]` | Drive the full pipeline over a recording or research `feed.jsonl` (always dry-run) | No |
| `ctc-record --out F [--symbols ...] [--max-frames N]` | Record the live market-data feed to JSONL | No (read-only stream) |
| `ctc-bot [--step-interval S] [--live]` | Run the live trading loop | `--live` additionally requires `CTC_DRY_RUN=false` and `CTC_AUTO_TRANSITION=true`; each order still waits for the controller |
| `ctc-build-live` | Refit the forecaster + priors on all seasons, save packaged model artifacts | No (writes local model JSONs) |

### Running the bot in dry-run (safe default)

```bash
ctc-bot                      # connects, subscribes to news+quotes, logs decisions, submits nothing
```

In dry-run the bot does the full real pipeline — ingests the live feed, updates the
estimator, computes fair values, runs the strategy and all risk checks — and logs every
order it *would* place (`DRY-RUN would submit: ...`) without ever calling the exchange's
write endpoints.

### Staged live rollout (currently not ready to activate)

Live submission requires all three opt-ins, then the 30-second observation, verified
execution test, fresh all-symbol quotes, healthy APIs/process, positive net edge, complete
session PnL, reconciled order ownership, and enforced limits:

```bash
export CTC_DRY_RUN=false      # in .env or the shell
export CTC_AUTO_TRANSITION=true
ctc-bot --live                # the --live flag AND CTC_DRY_RUN=false
```

The automatic gate defaults off. The case packet/starter kit does not provide a verified
starting-equity and realized-fill history source, so without a separately verified team
adapter (`CTC_ACCOUNTING_PROVIDER=module:function`) the production PnL gate remains
incomplete and this process cannot reach `LIVE`. The runner persists a one-shot PORT test
attempt and requires an acknowledged order ID, matching open-order readback, acknowledged
cancellation, absence after cancellation, and unchanged PORT position. Failed or incomplete
attempt evidence enters safe mode and cannot be silently retried. The test is the specific
one-lot place/cancel described in [TESTNET.md](TESTNET.md), only when executable quotes
exist; the new code has only been exercised with mocks here, not against the live exchange.
Do not enable live configuration until the adapter and all documented gates are verified.

## Persistent runtime state

When started, the bot writes private local JSON state (mode `0600`) to
`research/runtime/order_registry.json` and `research/runtime/session_pnl.json`. That folder
is gitignored. Back it up securely between restarts; never copy secrets into these files.
The registry records intents before POST, then exchange IDs, explicit fills and confirmed
cancellations. Any ambiguous submission or mismatch blocks ownership verification. A
restart always requires a fresh reconciliation. A partial `filled` count without fill
prices is not a realized-PnL record.

## Process visibility and controlled restart

In the Codex shell for this session, both `ps` and `pgrep` returned `Cannot get process list`
after a `sysmond service not found` error, and Codex reported that no app terminal session
was attached. This is a missing process-monitoring service in the shell integration, not an
error that the screenshots' Screen Recording or Accessibility switches can fix. No bot was
stopped or restarted as a workaround.

The updated runner now takes a POSIX non-blocking lock at `research/runtime/bot.lock`
before opening REST or WebSocket connections; a second updated instance exits without
connecting. This does not identify the older running process because it predates the lock,
so verify that PID manually before a controlled restart.

To establish process visibility from the Mac itself, open **Terminal.app** and run:

```bash
pgrep -fl '[c]tc-bot'
ps -p <PID> -o pid=,etime=,command=
```

Replace `<PID>` with the number printed by `pgrep`. If `pgrep` works in Terminal.app, use
that terminal for the later controlled deployment: send `SIGINT` to the confirmed PID,
verify it disappears with `pgrep`, then start exactly one replacement and verify one PID.
Do not use `pkill` or start a replacement while the old PID remains. If Terminal.app also
cannot list processes, save the exact error and ask the local Mac administrator to restore
the OS process-inspection service; this session has no evidence supporting a particular
macOS privacy toggle. Reopen/reattach a Codex local terminal only after Terminal.app proves
the host itself can enumerate processes.

### Kill switch

Set `CTC_KILL_SWITCH=true` (or call `trader.engage_kill_switch()`): the risk manager then
rejects every candidate order, so nothing new is proposed or submitted, even in live mode.
On shutdown the bot cancels all resting orders (live mode only).

## Offline replay (no network)

Replay a research feed through the exact live interfaces:

```bash
ctc-bot-replay data/raw/hurricane-research/hurricane-season-research/history/season-001/feed.jsonl
```

Research `feed.jsonl` files carry no quotes, so `proposed_orders` is 0 — the run still
exercises feed parsing, seq dedup, season tracking, and the estimator. To see signal
generation offline, replay a recorded **testnet** session (which includes quotes), or see
the deterministic end-to-end simulation in `tests/test_exec_e2e.py`.

## Recording & verifying a feed

```bash
ctc-record --out research/recordings/session.jsonl --symbols PORT,HAVEN,ARCH
# then verify against the starter-kit checker:
python data/raw/.../starter_kit/check_recording.py research/recordings/session.jsonl
```

Recordings are gitignored (`research/recordings/`, `*.jsonl`) — do not commit them.
