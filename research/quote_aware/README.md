# Quote-aware research candidate

See [integration review](../../docs/QUOTE_AWARE_REVIEW.md). This is a separate
research environment (Python 3.11+, scikit-learn 1.8.0); the existing `ctc-bot`
continues to use its original model. No raw history or account credentials included.

# CTC 2026 - Systematic Equities

**Connection correction, 24 October 2026:** use
`https://cornelltradingcompetition.org`. The supplied organizer conversation
confirms this host resolved the team's API authentication error. The bot,
inspector and recorder examples now use it. Its public case matches the
research companies, baskets, limits and timings exactly. The different case
and Google login error on the hurricane subdomain do not describe this host.
The case guard remains enabled. Authenticated account/feed behavior still
needs validation with a team key locally.

This package analyzes the supplied Hurricane Season case and 600 research
seasons. Read **PLAYBOOK.md** first, then use the offline replay. The exchange
adapter starts in **dry-run** mode. No team credentials were supplied and no
exchange orders were placed during development.

## Files

| File | What it does |
|---|---|
| `PLAYBOOK.md` | Valuation formulas, trading examples, results and operator guide |
| `core.py` | Public-feed state, inferred valuations, sizing, ETF arithmetic |
| `bot.py` | Quote-aware predictor plus exchange connection and order controls |
| `model.joblib` | Trained model; only load this package's trusted model file |
| `calibration.json` | Validation errors, selected thresholds and feature schema |
| `case.json` | Supplied map, sites, baskets, timings and limits |
| `control.json` | Pause switch, blocked tickers, explicit manual fair values |
| `inspect_exchange.py` | Read-only inspection of account, clock and order book |
| `replay.py` | Offline replay using only messages released so far |
| `research.py` + `refine.py` | Reproduce the benchmark and quote-aware challenger |
| `recover_parameters.py` | Re-estimate valuation coefficients from the raw history |
| `research_results.json` | Complete evaluation results and limitations |
| `evaluation_seasons.csv` | One row per evaluation season, including symbol PnL |
| `test_core.py` | Financial, state, deduplication and adapter tests |
| `reference/` | Organizer recorder, checker and loader; host examples corrected |
| `auth_diagnostics/` | Read-only connection checker and host-resolution notes |

## Install

Use **Python 3.11 or later**. The scikit-learn version is pinned to the version
used to write the model.

```bash
cd systematic_equities
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v test_core
```

Keep the original `hurricane-season-research` directory from the supplied ZIP.
It is not duplicated in this package.

```bash
python replay.py --data /path/to/hurricane-season-research --season 501
```

This creates `replay_values.csv`. It does not connect to the exchange. Historic
testnet seasons are for operational verification, not evidence of live-season
forecast skill. The strategy does not look up a season by date or storm name.

## Read-only exchange check and dry run

Set `CTC_API_KEY` in your local shell using the key from your team's captain.
Do not put it into a source file. In Bash you can enter it without echoing it:

```bash
read -r -s -p 'Team bot key: ' CTC_API_KEY
export CTC_API_KEY
python inspect_exchange.py
python bot.py
```

The default host is the one confirmed by the organizer conversation:
`https://cornelltradingcompetition.org`. REST and WebSocket use the same host:

```bash
python bot.py --url https://cornelltradingcompetition.org
```

The feed URL is `wss://cornelltradingcompetition.org/ws/v1/market-data`.
The API uses the `X-API-Key` header. A team join code is not an API key.

The API packet does not fully document response shapes. `position_map`,
`open_order_list` and the clock parser explicitly stop on unknown shapes.
Inspect the actual JSON and adjust those adapters if needed. This is the
remaining integration work that could not be verified without team access.

The bot logs `valuation`, `would_order`, `blocked`, and `announcement_halt`.
Its dry run reads the account but does not create simulated fills or send any
order. Compare the proposed trades to the displayed live bid/ask and depth.

## Execution status in this repository

The standalone research adapter rejects `--execute`. It can only run in dry-run.
Use the repository's reviewed `ctc-bot` stack for eventual execution, with both
existing live gates and explicit authorization. This model has not been wired
into that stack. Do not run two order-sending bots on the same account.

## Pause, announcements and recovery

Change `control.json` while the bot is running:

```json
{
  "paused": true,
  "blocked_symbols": [],
  "fair_value_overrides": {},
  "acknowledged_announcements": [],
  "allow_historical_model_after_announcement": false
}
```

Pausing cancels identifiable resting orders submitted by this process; it does
**not** flatten positions. Check the terminal after a failed cancellation or an
ambiguous order response. Existing positions still have economic exposure.

An announcement stops new trading until its logged `competition_id:seq` is
acknowledged. After acknowledgment, the default permits only symbols with
explicit manual fair-value overrides. Acknowledging a headline alone does not
update the simulator economics. Set `allow_historical_model_after_announcement`
only after assessing that the old model remains valid; otherwise revise the
model/overrides and affected ETF values. No announcement parser has been trained
because the research data has no announcements.

The bot de-duplicates sequences, retains damage across rounds, and resets on a
new competition ID. On disconnection it stops trusting integrated outages.
After receiving all three final reports, it can still value ISLE, AQUA, RECON,
MAT, HAVEN and REIN from reports alone (REIN is blocked above the historical
loss range). PORT, GRID, FERRY, GEN and both ETFs require complete outage
history. The bot does not reconstruct a full season from the sparse snapshot
or automatically replay the journal on restart.

Unknown/ambiguous order responses stop further submissions. Confirm the account
in the terminal before restarting; blindly retrying a timed-out POST can double
an order. The bot uses one connection, stale-quote checks and a local rolling
budget of at most 120 actions/minute. Other team keys still share the exchange's
180 actions/minute limit.

ETF creation/redemption opportunities are **alerts only**. The arithmetic checks
the correct sides of the quoted markets and temporary conversion inventory;
it does not verify depth or execute a multi-leg basket. Neither the packet nor
the data supplies an atomic basket-order facility.

## Reproduce the research

```bash
python recover_parameters.py --data /path/to/hurricane-season-research
python research.py --data /path/to/hurricane-season-research --rebuild
python refine.py
```

The last two commands rewrite the model and results. Run them in a copy if you
want to preserve the delivered artifacts. Training uses seasons 1-400,
validation/threshold selection uses 401-500, and evaluation uses 501-600.
Model refinement followed review of the first benchmark, so the final
evaluation is a season-disjoint development evaluation, not an untouched
one-shot external validation. The report states this explicitly.

The backtest assumes fills for the full requested size at a later house
consensus plus an assumed cost, when the submitted limit would permit it.
Unfilled orders are canceled, costs apply to both sides, positions carry
between rounds, and remaining inventory settles only after round 3.
No books, queue priority, partial fills, opponent flow or market impact are
available. Never interpret the reported proxy PnL as verified tradable profit.
