# Bot Operations

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
# CTC_HTTP_URL=https://hurricane.cornelltradingcompetition.org
# CTC_MAX_ORDER_SIZE_FRAC=0.25     # per-order cap as fraction of the symbol position limit
# CTC_MAX_GROSS_EXPOSURE=2000000   # max |notional| across the book ($)
# CTC_MAX_ORDERS_PER_MIN=50        # below the 60/bot exchange ceiling
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
| `ctc-bot [--step-interval S] [--live]` | Run the live trading loop | **Only if `CTC_DRY_RUN=false` AND `--live`** |
| `ctc-build-live` | Refit the forecaster + priors on all seasons, save packaged model artifacts | No (writes local model JSONs) |

### Running the bot in dry-run (safe default)

```bash
ctc-bot                      # connects, subscribes to news+quotes, logs decisions, submits nothing
```

In dry-run the bot does the full real pipeline — ingests the live feed, updates the
estimator, computes fair values, runs the strategy and all risk checks — and logs every
order it *would* place (`DRY-RUN would submit: ...`) without ever calling the exchange's
write endpoints.

### Going live (guarded, requires human approval)

Live submission is intentionally hard to trigger. **Both** are required:

```bash
export CTC_DRY_RUN=false      # in .env or the shell
ctc-bot --live                # the --live flag AND CTC_DRY_RUN=false
```

If `--live` is passed while `CTC_DRY_RUN` is still true, the bot refuses to start. Do not do
this without explicit approval and after the single place+cancel test in
[TESTNET.md](TESTNET.md) has passed.

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
