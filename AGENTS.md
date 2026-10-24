# AGENTS.md — working in this repo

> Update, 2026-10-24: the team reported HTTP 200 after switching to
> `https://cornelltradingcompetition.org`; earlier key-blocker statements below
> refer to the old hurricane subdomain and are superseded. Defaults are fixed;
> local `.env` overrides must also be updated. Authenticated feed/order behavior
> remains unverified by this agent. See [quote-aware review](docs/QUOTE_AWARE_REVIEW.md).

> Update, 2026-10-24: rollout safety infrastructure now includes the persistent order
> registry, session PnL tracker, and gated transition controller. Live activation remains
> disabled because the official interfaces do not yet provide verified equity/fills
> accounting. The PORT test is authorized only conditionally on executable quotes and has
> not been run. Current full-suite baseline: 173 tests.


Guide for AI/human agents continuing the CTC 2026 systematic-equities bot. Read this plus
[docs/HANDOFF.md](docs/HANDOFF.md) before making changes.

## Golden rules (do not violate)

1. **Never expose the bot key.** `CTC_API_KEY` lives only in `.env` (gitignored, mode 600).
   Never print, log, commit, or paste it. All tooling already redacts it; keep it that way.
2. **Dry-run is the default and must stay so.** Do not change the default of `CTC_DRY_RUN` to
   false. Staged live submission requires `CTC_DRY_RUN=false`, `ctc-bot --live`,
   `CTC_AUTO_TRANSITION=true`, and every controller gate to pass.
3. **No state-changing exchange calls without explicit human approval** — no placing/
   cancelling orders, no ETF create/redeem. The only conditionally authorized test is the
   one-lot PORT place+cancel in [docs/TESTNET.md](docs/TESTNET.md), and it must not be run
   without executable quotes; no broader live trading is authorized by that test.
4. **Do not invent endpoints, message shapes, exchange rules, or results.** Use the case
   packet (`docs/*.pdf`) and starter kit (`data/raw/.../starter_kit/`) as source of truth.
   Record real probe/test results honestly, including failures.
5. **Branch:** work on `feat/research-baseline`. **Do not merge into `main`.**
6. **Do not commit** `.env`, API keys, the raw 600-season dataset, or large feed recordings
   (`research/recordings/`, `*.jsonl` are gitignored; `data/raw/` is large).

## Environment

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,live]"
```

Python 3.13 locally (CI covers 3.10–3.13). The raw dataset is optional — the live bot runs
off packaged model artifacts (`src/ctc/models/*.json`); data-backed tests auto-skip without
`data/raw/` or `CTC_DATA_DIR`.

## Before marking any work complete

```bash
ruff format src tests
ruff check src tests          # must pass (line length 110)
pytest -q                     # 173 tests must pass (plus 17 separate quote-aware checks)
```

CI (`.github/workflows/ci.yml`) runs lint + format + pytest on 3.10–3.13.

## Where things live

* Live stack: `src/ctc/execution/` — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
  module map and data flow.
* Models/research reused live: `forecasts/estimator.py` (`build_live_estimator()`),
  `strategy/signals.py` (`decide_live`), `risk/limits.py`.
* Tests: `tests/test_exec_*.py` (execution, no network — uses injected fakes) and the rest of
  `tests/` (research). `tests/test_exec_e2e.py` is a deterministic end-to-end simulation.
* Docs: `docs/` — ARCHITECTURE, EXCHANGE, BOT_OPERATIONS, RISK, TESTNET, CHECKLIST, HANDOFF,
  RESEARCH.

## Conventions

* Keep the decision stage (`LiveTrader.propose`) pure/testable; only `execute()` does I/O.
* Make exchange I/O testable via injected `transport` / `ws_connect` (no live network in
  tests).
* When you confirm a real live field name or behavior, tighten the tolerant parser in
  `models.py` and update the "Confirmed vs assumed" section of [docs/EXCHANGE.md](docs/EXCHANGE.md).
* Commit in small logical units with clear messages; push to `feat/research-baseline`
  frequently so work is preserved.

## Current status (keep this honest)

Research done; live stack and staged rollout controls built and tested (173 tests). Record→replay→dry-run round-trip
verified offline. Corrected host `https://cornelltradingcompetition.org` accepts read-only
REST auth. The latest reported competition state was `ready`, with flat positions and an empty
PORT book. The current Codex shell could not enumerate processes (`sysmond service not found` /
`Cannot get process list`); do not restart until the process is identified from Terminal.app.
Live PnL remains incomplete because the official interface has no verified account-equity and
realized-fill history source. The quote-aware teammate candidate remains research-only. See
[docs/CHECKLIST.md](docs/CHECKLIST.md) and [docs/BOT_OPERATIONS.md](docs/BOT_OPERATIONS.md).
