# Pre-Event Checklist

Official "Before the event" checklist (systematic-equities case packet) with current status.
Legend: ✅ done · 🟡 implemented, pending live credential · ⛔ blocked/needs human.

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Download research data, read README | ✅ | 600 seasons under `data/raw/` |
| 2 | Load data, run reference code | ✅ | `ctc-eda` reproduces advisory accuracy (69.6%) |
| 3 | Build a valuation model per company | ✅ | All 12 symbols; held-out MAE < 1% of start for 10/12 |
| 4 | Measure forecast accuracy & how early patterns appear | ✅ | `ctc-forecast-eval` → `forecast_eval.json` |
| 5 | Sign in on testnet, join team, get a bot key | ⛔ | Key present but **rejected (401)** by exchange — see [TESTNET.md](TESTNET.md) |
| 6 | Connect bot, subscribe to news, place + cancel an order | 🟡 | Client + bot implemented and tested offline; place+cancel is the proposed first live test (awaiting key + approval) |
| 7 | Stay connected across season rollovers | 🟡 | Reconnect + `on_new_season()` implemented and unit-tested; needs live verification |
| 8 | Record a season + verify with `check_recording.py` | 🟡 | `ctc-record` writes a check-compatible JSONL; verify once a live key works |
| 9 | Trade a full testnet season by hand and with the bot | ⛔ | Depends on 5–8 and explicit approval |

**Summary:** all research items (1–4) are complete and verified. The live stack (client,
state, risk, trader, recorder, probe, replay) is **built and tested offline** (128 tests
passing); items 5–9 are blocked only on a **valid/active bot key** and explicit approval for
state-changing actions.

## Engineering checklist (this repo)

* ✅ 128 tests passing (`pytest -q`), including deterministic end-to-end simulation.
* ✅ Lint + format clean (`ruff check`, `ruff format --check`).
* ✅ CI (GitHub Actions): lint + format + pytest on Python 3.10–3.13.
* ✅ Dry-run is the default; live submission requires `CTC_DRY_RUN=false` **and** `--live`.
* ✅ `.env` gitignored (mode 600); key never logged/printed/committed.
* ✅ Read-only probe run and recorded honestly (public 200, auth 401).
* 🟡 Strategy improvements on held-out seasons (Phase 8) — baseline kept; in progress.

## Immediate next actions (for whoever resumes)

1. Get an **active** bot key into `.env`; re-run `ctc-bot-probe` until `auth_time.ok == true`.
2. With approval, run the single place+cancel test in [TESTNET.md](TESTNET.md).
3. Run `ctc-bot` (dry-run) against the live feed for a full season; confirm reconnect across
   a rollover and record a session; verify with `check_recording.py`.
4. Continue Phase 8 strategy work (see [HANDOFF.md](HANDOFF.md)).
