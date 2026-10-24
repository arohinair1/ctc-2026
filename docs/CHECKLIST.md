# Pre-Event Checklist

> Update, 2026-10-24: the team reported HTTP 200 after switching to
> `https://cornelltradingcompetition.org`; earlier key-blocker statements below
> refer to the old hurricane subdomain and are superseded. Defaults are fixed;
> local `.env` overrides must also be updated. Authenticated feed/order behavior
> remains unverified by this agent. See [quote-aware review](QUOTE_AWARE_REVIEW.md).


Official "Before the event" checklist (systematic-equities case packet) with current status.
Legend: ✅ verified · 🟡 implemented or connected, pending live validation · ⛔ blocked/needs human.

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Download research data, read README | ✅ | 600 seasons under `data/raw/` |
| 2 | Load data, run reference code | ✅ | `ctc-eda` reproduces advisory accuracy (69.6%) |
| 3 | Build a valuation model per company | ✅ | All 12 symbols; held-out MAE < 1% of start for 10/12 |
| 4 | Measure forecast accuracy & how early patterns appear | ✅ | `ctc-forecast-eval` → `forecast_eval.json` |
| 5 | Sign in on testnet, join team, get a bot key | ✅ | Read-only authenticated REST succeeds on corrected host; see [TESTNET.md](TESTNET.md) |
| 6 | Connect bot, subscribe to news, place + cancel an order | 🟡 | One dry-run bot process connected; order round-trip remains untested and needs explicit approval |
| 7 | Stay connected across season rollovers | 🟡 | Reconnect + `on_new_season()` implemented and unit-tested; needs live verification |
| 8 | Record a season + verify with `check_recording.py` | 🟡 | `ctc-record` writes check-compatible JSONL; live full-season capture remains |
| 9 | Trade a full testnet season by hand and with the bot | ⛔ | Depends on 5–8 and explicit approval |

**Summary:** research items (1–4) and read-only testnet authentication (5) are verified.
The live stack is implemented and tested (131 tests passing), with one dry-run bot process
connected. Live market data, approved order validation, full-season recording, rollover, and
the complete three-round testnet run remain.

## Engineering checklist (this repo)

* ✅ 131 tests passing (`pytest -q`), including deterministic end-to-end simulation.
* ✅ Lint + format clean (`ruff check`, `ruff format --check`).
* ✅ CI (GitHub Actions): lint + format + pytest on Python 3.10–3.13.
* ✅ Dry-run is the default; live submission requires `CTC_DRY_RUN=false` **and** `--live`.
* ✅ `.env` gitignored (mode 600); key never logged/printed/committed.
* ✅ Read-only probe on corrected host: public case, authenticated time, and positions all 200.
* ✅ Strategy improvements evaluated on held-out seasons (Phase 8) — `ctc-strategy-eval`;
  baseline kept as default (gates give only marginal held-out gains; see HANDOFF.md).

## Immediate next actions (for whoever resumes)

1. Wait for the competition to enter `running` and confirm live frames and nonempty quotes.
2. Keep the single bot in dry-run and verify fresh state updates; do not start a duplicate WS.
3. With explicit approval, run the single place+cancel test in [TESTNET.md](TESTNET.md).
4. Record a full season, validate with `check_recording.py`, then verify reconnect/rollover.
5. Complete the full three-round testnet run before considering any live sizing.
