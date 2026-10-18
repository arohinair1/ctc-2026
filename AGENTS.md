# AGENTS.md — working in this repo

Guide for AI/human agents continuing the CTC 2026 systematic-equities bot. Read this plus
[docs/HANDOFF.md](docs/HANDOFF.md) before making changes.

## Golden rules (do not violate)

1. **Never expose the bot key.** `CTC_API_KEY` lives only in `.env` (gitignored, mode 600).
   Never print, log, commit, or paste it. All tooling already redacts it; keep it that way.
2. **Dry-run is the default and must stay so.** Do not change the default of `CTC_DRY_RUN` to
   false. Live submission requires both `CTC_DRY_RUN=false` and `ctc-bot --live`.
3. **No state-changing exchange calls without explicit human approval** — no placing/
   cancelling orders, no ETF create/redeem. The one approved-in-advance *plan* (not yet run)
   is the 1-lot PORT place+cancel in [docs/TESTNET.md](docs/TESTNET.md); it still needs a
   human "go".
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
pytest -q                     # 128 tests must pass
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

Research done; live stack built and tested offline (128 tests). Blocked on a valid bot key
(current key → `401 Invalid API key`; public endpoint works). Next: get an active key, run
the probe to green, then the approved place+cancel test. See [docs/CHECKLIST.md](docs/CHECKLIST.md).
