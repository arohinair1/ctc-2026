# Integration review and quote-aware candidate

Reviewed starting commit `4a6e7663f97ac90c40383a8bc49b64611251b3a0` on
`feat/research-baseline`. Main contained only a placeholder README. The research
branch already had a full model/backtest pipeline, packaged JSON models,
REST/WebSocket clients, risk checks, recording/replay, and offline execution tests.

## Changes

- Correct the existing live stack's default REST host to
  `https://cornelltradingcompetition.org`; WebSocket derives from the same host.
  An old `CTC_HTTP_URL` in a local `.env` overrides this fix and must be updated.
- Correct the earlier diagnosis that the keys were necessarily inactive. The
  organizer conversation supplied by the user reports a keyed clock HTTP 200 on
  the main domain. Earlier failures on the hurricane subdomain remain documented
  as historical observations. No keyed check was performed in this integration.
- Add `research/quote_aware/`: model, calibration, causal state, inferred terminal
  formulas, reproducible training/replay, evaluation CSV/JSON, and read-only auth
  diagnostic. No raw history, account information or recordings are included.
- Keep the current `ctc-bot` strategy and both execution gates unchanged. The
  imported standalone adapter rejects `--execute`; it is a dry-run research
  prototype, not a second approved execution path.

## What differs from the existing research

| Area | Existing baseline | Added candidate |
|---|---|---|
| Forecast | SeasonEstimator with priors/damage forecast | ExtraTrees correction to current house midpoints |
| Rebuilders | Fitted square-root drivers | Inferred exponential saturation formulas |
| Season split | Seeded shuffle, 360/120/120 | Chronological 400/100/100 |
| Costs/fills | Synthetic percentage half-spread | Absolute dollar cost; delayed limit eligibility |
| Reported evaluation mean | About 1,099 | About 2,596 at $0.20/share/side and 10-second delay |
| Evaluation status | Existing documented test split | Development evaluation; initial results informed refinement |

These PnLs are **not a head-to-head comparison**. Seasons, sizing, fill timing and
cost units differ. Neither data source contains executable historical books,
queue priority, partial fills, competitor flow or market impact. Candidate win
rate is 74%, p05 -4,545 and worst season -11,114 under its stated proxy assumptions.

Model promotion requires a common evaluation harness: use identical seasons,
position/action limits, bid/ask assumptions, delayed fills, costs and settlement
accounting; then compare both strategies and stress outcomes. Do not select the
candidate simply because its reported mean is higher.

## Review findings that remain relevant

- The existing documentation calls the split chronological, but
  `src/ctc/data/splits.py` explicitly shuffles season IDs with a fixed seed before
  splitting. Season separation is preserved; chronological test separation is not.
- The existing REIN fitted layer has a finite width. The added inference does not
  identify an upper exhaustion point from observed data; it blocks exposure above
  its observed loss range. Treat extrapolation beyond that range as unresolved.
- The existing trader's shutdown calls `cancel_all()` when live and not killed.
  Coordinate account ownership before sharing an account with another trader.
- Both strategies still need actual protocol/feed verification. Successful REST
  authentication does not prove WebSocket message parsers or order behavior.

## Local use

For the existing bot, from the repository root:

```bash
python -m pip install -e '.[dev,live]'
ctc-bot-probe --listen 5
ctc-bot
```

Keep the API key in your local gitignored `.env`, with mode 600; keep
`CTC_DRY_RUN=true`. No credentials need to be sent to chat.

For candidate replay, use a separate Python 3.11+ environment:

```bash
cd research/quote_aware
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v test_core
python replay.py --data /path/to/hurricane-season-research --season 501
```

The `--data` directory contains `case.json` and `history/`; this differs from the
existing baseline's `CTC_DATA_DIR`, which points to the history directory itself.
The trusted included `model.joblib` was serialized with scikit-learn 1.8.0.
Do not load pickle/joblib files from untrusted sources.

No exchange orders, cancellations, conversions, team joins or key creations were
performed during this integration.

## Validation for this integration

- Existing pytest suite: 130 passed with the supplied historical data available.
- Candidate unittest suite: 17 passed.
- Ruff lint and format checks passed.
- Candidate `--execute` rejected before credential loading or network access.
- Candidate season 501 replay completed using the included serialized model.
- Git diff whitespace checks passed. Remote CI status is separate from these local checks.
