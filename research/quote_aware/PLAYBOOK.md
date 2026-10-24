# Repository integration note

This archived research guide describes the original standalone prototype. In
this repository `bot.py --execute` is disabled. The existing `ctc-bot` remains
the execution stack; the quote-aware model has not been promoted into it.

# CTC 2026: Systematic Equities



Hurricane Season | Research, strategy and operating guide | 24 October 2026

**Recommendation:** use the house price as the starting forecast, estimate its error using public storm information, and switch to the recovered valuation formulas once the final damage is known. Keep ETF conversion opportunities on a separate, executable-price checklist.

## What was actually completed

| Work | Result |

| --- | --- |

| Data audit | Read the full 17-page case packet and all 600 seasons: 1,800 storms. |

| Valuation | Recovered equations for all ten stocks and exact ETF basket relationships. |

| Forecasting | Trained a quote-aware model on 400 seasons; calibrated thresholds on 100 different seasons. |

| Evaluation | 100 additional seasons: mean proxy PnL $2,596, median $2,322, 74% profitable. |

| Implementation | Offline replay, trained model, dry-run bot, operator controls, source recovery and 17 passing offline checks. |

| Integration boundary | No team key was supplied. No live exchange connection, order, fill or conversion has been verified. |



## The three things worth remembering

**1. Track outages, not just damage.** FERRY loses $7.20 per weighted market hour closed, versus only $0.05 per percentage point of physical damage. GRID and GEN are also highly sensitive to downtime.

**2. Insurance is a season-total payoff.** HAVEN absorbs the $400M-$1,300M layer; REIN starts above $1,300M in the observed range. The same new storm can hurt one insurer and barely affect the other.

**3. A known value is not automatically an available trade.** The final-report-only strategy averaged just $1.36 under the delayed-fill, $0.20-cost proxy. Most information can already be in prices by then.

## How much confidence to place in the PnL

The $2,596 average assumes a 10-real-second execution delay, $0.20 cost per share on each side, and a 35% allocation of each published position limit. The worst season lost $11,114. There are no order books in the supplied history: depth, queue priority, partial fills, other teams and market impact are unobserved. This is a research estimate, not an exchange-verified return.


---


# 1. Recover the terminal values



These are **empirically inferred equations**, not parameters explicitly disclosed by the organizers. They fit the supplied historical economic regime. Announcements may invalidate them.

**D_X** is the sum of final damage percentages for company X across the three rounds, in percentage points: 20 means 20%, not 0.20. **H_X** is cumulative, site-weighted outage time in market hours. **L** is total season insured losses in millions of dollars.

| Stock | Estimated terminal value per share |

| --- | --- |

| PORT | 100 - 0.55 D_PORT - 2.4 H_PORT |

| ISLE | 60 - 0.38 D_ISLE |

| AQUA | 35 - 0.20 D_AQUA |

| GRID | 50 - 0.24 D_GRID - 6 H_GRID |

| FERRY | 25 - 0.05 D_FERRY - 7.2 H_FERRY |

| RECON | 40 + 24 [1 - exp(-0.009 D_PORT - 0.007 D_GRID - 0.0012 D_FERRY)] |

| MAT | 28 + 14 [1 - exp(-0.0075 D_PORT - 0.005 D_ISLE - D_AQUA/480 - 0.00583333 D_GRID - 0.001 D_FERRY)] |

| GEN | 32 + 18 [1 - exp(-H_GRID)] |

| HAVEN | 75 - 0.06 min(max(L - 400, 0), 900) |

| REIN | 90 - 0.035 max(L - 1300, 0), within the observed loss range |



A useful cross-check is L approximately equal to 9 D_PORT + 6 D_ISLE + 2.5 D_AQUA + 7 D_GRID + 1.2 D_FERRY. Prefer the explicit loss reports when available; both damages and losses are rounded in the feed. The reinsurance layer's upper exhaustion point is not identified by these data.

## Estimate outage time directly from observations

The closure rule that best fits the settlement data uses wind strength W = intensity x exp(-distance_squared / radius_squared), with the site closed when W &gt; 0.35. Integrate the closure indicator through time and multiply by the site weight. One real second is 30 market seconds, so **H_X = sum(weight x closed real seconds) / 120**. The code uses trapezoidal integration of the two-second observations.

Do not add a preliminary report to a final report for the same storm. Replace the preliminary estimate. The reported damage is already the incremental contribution to the company's asset value; sum the final percentages across rounds without applying an extra compounding adjustment.


---


# 2. Turn the feed into information



| Signal | How to use it |

| --- | --- |

| Storm observations, every 2 s | Treat x, y, velocity, radius and intensity as measurements. Update wind at each of the 17 sites and integrate outage exposure. |

| Track, every 20 s | Use to inspect the trajectory and diagnose missing observations. Sparse track points do not perfectly reconstruct all outage hours. |

| Official advisory | Use outlook, island probabilities and forecast cone together with the observed path. Island hit probability is not expected company damage. |

| Wire rumor | A weaker signal. Its value depends on what the market already priced and whether current observations support it. |

| Preliminary report | An estimate; retain uncertainty. The final report replaces it. |

| Final report | Exact reported incremental damage and insured loss. After round 3, combine all three reports and accumulated outages. |

| Announcement | Potential parameter change. Pause new trading and revalue the affected businesses and dependent securities. |



## Measured credibility in the 400 training seasons

| Reported outlook | Official: P(correct | report) | Wire: P(correct | report) |

| --- | --- | --- |

| Direct strike | 75.9% | 48.1% |

| Island chain | 76.7% | 48.7% |

| Recurve | 76.5% | 48.1% |

| Stall | 68.6% | 35.1% |

| Eyewall replacement | 65.4% | 35.3% |

| Erratic | 36.6% | 13.9% |

| All messages | 69.9% of 9,600 | 39.6% of 12,200 |



For truth labels, a storm is erratic after its recorded switch time, rather than retaining its original pattern. Repeated advisories are not independent experiments: do not multiply the same 70% credibility over and over. A reported "erratic" outlook has a much lower posterior accuracy than the overall official hit rate.

Most storms initially move north; direct, chain and recurve can look alike before the turn. In the training set, 74 of 1,200 storms later switched to erratic. Use changes in the measured path to reassess an outlook. Do not assume the first advisory determines the rest of the round.


---


# 3. Know what each island exposes



| Island | Economic concentrations |

| --- | --- |

| Cascadia Prime | PORT 100%; GRID 50%; FERRY 30%. The port is at (470, 80), while the ferry terminal is farther south at (380, 185). |

| Coral Key | ISLE 34%; AQUA 45%; GRID 18%; FERRY 25%. |

| Azure Isle | ISLE 34%; GRID 17%; FERRY 20%. |

| Palm Atoll | ISLE 32%; AQUA 55%; GRID 15%; FERRY 25%. |



Coordinates matter within an island. The sites are not colocated. A storm clipping a ferry terminal may miss a resort or substation, and the PORT site lies well north of the other Cascadia assets. The map origin is at the upper left: decreasing y means north. Velocity is measured in map units per real second.

## Company-specific effects

**ISLE:** structural damage and the associated booking effect are captured by an effective $0.38 loss per damage point in the historical regime. The unaffected value of $60 is not the initial traded price, which already anticipates the season.

**AQUA:** two farms cannot move; fleets can shelter. Do not translate the company's island weights into identical vulnerability at every site. The direct damage model was less reliable than the recovered terminal valuation equation, so it is not used as an exact forecast.

**FERRY and GRID:** a long moderate storm can be costly even without extreme structural damage. FERRY loses $7.20 per weighted hour, GRID $6.00. An island-wide "impact" alert is insufficient to recover the duration of each site closure.

**RECON and MAT:** both saturate. The marginal dollar of extra damage generates less incremental value after a large prior workload. RECON depends on port, grid and ferry repairs; MAT draws on all five physical businesses. Their asymptotic values are $64 and $42 respectively in the recovered regime.

**GEN:** grid downtime, rather than aggregate insured losses, is the useful driver. One weighted hour of outage implies 32 + 18(1 - exp(-1)) = about $43.38. Two hours implies about $47.56; it approaches $50.

## Why the remaining storms matter

Round 1 and round 2 reports do not reveal settlement values. Existing positions, damage and season losses carry forward. Forecasts must include storms that have not yet formed. The quote-aware model learns a conditional expectation of the final settlement, rather than pretending the current-condition valuation is the last payout.


---


# 4. Trade price errors, with a buffer



The delivered model starts with the current quoted midpoint and predicts a correction using 246 public-input features: round and elapsed time, observed wind integrals, reported damage and losses, current and lagged storm observations, latest advisories and cone, advisory counts, and current prices. Calendar date, season identity, storm name, true manifest pattern and future messages are excluded.

The first independent forecast had larger errors than the house and weak delayed-execution results. The delivered model is quote-aware. It is still **not uniformly more accurate than the house on every stock and round**; valuation accuracy and the profitability of selected deviations are different tests.

## Implemented decision rule

For symbol i, take sigma from validation root-mean-square prediction errors for that round and phase. The selected threshold is T = max($0.20, 0.10 x sigma). Buy only when fair value - ask &gt; T; sell only when bid - fair value &gt; T. Cap the target at 35% of the symbol limit; the target grows with the excess edge. This is an aggressive expected-profit setting, not a high-confidence probability bound.

Submit marketable limit orders at the observed ask or bid. Reconcile remaining orders and actual positions before reordering. A limit price is a maximum buy/minimum sell price, not a promise of execution. The loop uses stale-quote checks and explicit order IDs. An ambiguous POST response stops new submissions rather than automatically resending.

## Worked value trade: FERRY

Suppose final cumulative damage is 30 percentage points and weighted outage time is 1.2 hours. Fair value = 25 - 0.05(30) - 7.2(1.2) = **$14.86**. If the executable bid is $16.00, selling 100 shares and holding to unchanged settlement would produce $114 before additional costs. If the only available bid is $14.80, there is no short edge from this calculation.

## Do not value an insurance layer at the mean loss

Suppose two equally likely terminal season losses are $1,200M and $1,400M. HAVEN is worth $27 or $21, so its expected value is **$24**. Using the mean loss of $1,300M would incorrectly give $21. REIN is worth $90 or $86.50, so its expected value is **$88.25**; pricing only at the mean would incorrectly give $90. Average the payoff across scenarios before placing the trade.

The supervised model predicts terminal stock values directly, which lets it learn this nonlinear expectation. Once all final reports arrive, no averaging over future losses is needed. REIN is automatically blocked when observed season losses exceed the historical maximum of $1,766.2M because its upper-layer behavior has not been identified.


---


# 5. ETF conversions: use tradable sides



**ARCH** = PORT + ISLE + 2 AQUA + GRID + 2 FERRY + HAVEN.
**REBLD** = 2 RECON + 2 MAT + GEN. These are share baskets, not normalized averages.

| Direction | Price condition before costs and slippage | Inventory sequence |

| --- | --- | --- |

| ETF expensive | ETF bid > sum(component quantity x component ask) | Create ETF (+ETF, -basket), then sell ETF and buy components; or assemble the basket first if limits require. |

| ETF cheap | sum(component quantity x component bid) > ETF ask | Redeem ETF (-ETF, +basket), then buy ETF and sell components; or buy the ETF first if limits require. |



Create/redeem is immediate and fee-free while the market is open, and it can create short positions within limits. It is one order action. The subsequent order-book trades are separate actions and are not atomic. Check depth and the inventory after **every intermediate leg**. A midpoint basket gap is not sufficient.

## Worked conversion

Suppose RECON ask is $52.10, MAT ask $35.20, GEN ask $40.10, and REBLD bid $216.00. The basket costs 2(52.10) + 2(35.20) + 40.10 = **$214.70**. Selling one REBLD and buying its basket produces a quoted $1.30 difference. Creation neutralizes the basket/ETF inventory if all trades complete at those prices. Four orders plus one conversion use five actions; additional placements or cancellations cost more.

The supplied bot reports these opportunities but does not automatically execute them. Without verified depth and fill-response schemas, an automatic multi-leg executor would hide significant leg risk. If a component reprices before all fills, the original quoted difference may disappear.

## Position limits and overlap

| Symbol | Limit | 35% model allocation | Symbol | Limit | 35% model allocation |

| --- | --- | --- | --- | --- | --- |

| PORT | 200 | 70 | RECON | 500 | 175 |

| ISLE | 300 | 105 | MAT | 700 | 245 |

| AQUA | 500 | 175 | GEN | 600 | 210 |

| GRID | 400 | 140 | HAVEN | 250 | 87 |

| FERRY | 800 | 280 | REIN | 200 | 70 |

| ARCH | 50 | 17 | REBLD | 100 | 35 |



Limits apply to long and short positions and same-side resting orders. Owning ARCH and its stocks increases combined exposure; it is not diversification from those stocks. The default allocation is a per-symbol cap, not a portfolio risk limit. Assign one operator/bot to each symbol or explicitly coordinate allocations.


---


# 6. What the evaluation actually says



Train: seasons 1-400. Validation and threshold selection: 401-500. Evaluation: 501-600. Splits are by complete season, so different points of one storm never enter both training and evaluation. Refinement followed review of the first benchmark; this is a season-disjoint development evaluation, not an untouched external test.

| Assumed cost per side | Delay | Mean PnL | Median PnL | Profitable |

| --- | --- | --- | --- | --- |

| $0.05 | 10 s | $3,523 | $3,407 | 79% |

| $0.20 | 10 s | $2,596 | $2,322 | 74% |

| $0.50 | 10 s | $1,644 | $1,526 | 65% |

| $1.00 | 10 s | $929 | $495 | 55% |

| $0.20 | 0 s | $4,209 | $4,165 | 86% |



For the $0.20 / 10-second scenario: 5th percentile = $-4,545, worst = $-11,114, best = $12,417. A positive mean does not eliminate the chance of a damaging single competition season.

## Terminal formula errors, after full history and final reports

| Symbol | Mean absolute error per share | Symbol | Mean absolute error per share |

| --- | --- | --- | --- |

| PORT | $0.0089 | MAT | $0.0025 |

| ISLE | $0.0025 | GEN | $0.0207 |

| AQUA | $0.0027 | HAVEN | $0.0024 |

| GRID | $0.0132 | REIN | $0.0007 |

| FERRY | $0.0198 | ARCH | $0.0429 |

| RECON | $0.0025 | REBLD | $0.0217 |



These are valuation errors, not PnL, and use complete observed paths with no economic announcements. The small remaining errors are consistent with feed rounding and two-second approximation of closure intervals.

## Fill assumptions that still need testing

Every ten seconds the simulator selects at most five candidate orders. Orders attempt execution at the next sampled house price plus/minus the assumed cost, only if the original limit permits it. Unfilled orders are canceled; costs apply both ways; positions carry across rounds and settle after round 3. The local rolling action budget is enforced. Full requested quantity is assumed available.

**Not modeled:** actual bid/ask width, depth, queue priority, partial fills, price protection, the bot's own market impact, other teams, API latency distribution, and live announcements. The live bot polls more frequently and real response formats still need verification. The proxy is not an exact exchange replay.


---


# 7. Run it and operate the round



Use Python 3.11+. Unzip the package, create a virtual environment, install requirements, and run the offline checks. The README contains the complete commands and troubleshooting guidance.

python -m pip install -r requirements.txt
python -m unittest -v test_core
python replay.py --data /path/to/hurricane-season-research --season 501

Set CTC_API_KEY in your local environment. Start with **python inspect_exchange.py**, then **python bot.py**. The latter is dry-run by default. Confirm that the positions, orders and clock parsers accept real responses. The packet supplies request examples but not all response schemas; unknown shapes stop execution.

Only after operational tests: **python bot.py --execute --max-fraction 0.10**. This submits orders to the competition simulation. No exchange orders were sent during development. The default host is https://cornelltradingcompetition.org, confirmed in the organizer conversation. Its public case matches the research configuration.

| Real time from open | Market clock | What to do |

| --- | --- | --- |

| 0:00 | 09:30 | Verify connection, competition ID, zero/reset account state and fresh quotes. |

| 1:00 | 10:00 | Storm forms. Start integrating all 17 site exposures. |

| 1:30, then every minute | 10:15, then every 30 market min | Read official advisory; compare it with the observed path and current prices. |

| 9:00 | 14:00 | Storm dissipates. Retain all damage and outage history. |

| 9:30 | 14:15 | Preliminary report arrives; do not treat it as exact. |

| 12:20 | 15:40 | Final report arrives. In round 3 switch to direct terminal formulas. |

| 13:00 | 16:00 | Round closes; only the end of round 3 is final settlement. |



## Operational controls

The team limit is 180 actions/minute, even though an individual bot key allows 300. The bot caps itself at 120; other keys and manual activity must fit the shared remaining allowance. One placement, cancellation, cancel-all or conversion each counts as an action. Use one WebSocket subscription with all channels; the exchange ignores subsequent subscription messages.

Set paused=true in control.json to stop new orders and cancel identifiable orders from this process. This does not flatten positions. On an announcement, all new trading pauses. After acknowledgment, only explicit manual fair-value overrides trade by default. Review affected insurers, rebuilders and ETF constituents as well as named tickers.


---


# 8. Failure modes and final checklist



| Failure | Required response |

| --- | --- |

| Duplicate message | Ignore a sequence already processed. Sequence gaps alone are normal. |

| Missing storm observations | Stop relying on integrated outages. A sparse snapshot is not a complete historical path. |

| Late connection / restart | Recover reports from snapshots. After all final reports, report-only formulas can value ISLE, AQUA, RECON, MAT, HAVEN and REIN. Outage-dependent instruments remain blocked. |

| New competition ID | Reset season state, seen sequence, quotes and positions assumptions. Keys can remain valid across testnet seasons. |

| Uncertain POST outcome | Inspect account and open orders. Do not blindly repeat the request. |

| Stale or crossed quote | Do not submit using it. The bot rejects stale quotes and crossed bid/ask inputs. |

| Changed economics | Pause. Historical equations cannot automatically price arbitrary announcements. |

| ETF partial completion | Know the remaining stock/ETF inventory and hedge it deliberately. Never call an unfinished basket risk-free. |



## What still has to happen on your team account

Check actual REST response schemas, WebSocket quote fields, spread and depth, partial fills, position reservations, cancel behavior, ETF conversion accounting, and the round/season reset. Record a testnet season with the organizer's tools and check that it matches the published feed. Start the live process before the first storm so all outage intervals are observed.

## Research artifacts and reproducibility

core.py contains the formulas and causal state. model.joblib and calibration.json contain the trained predictor and validation thresholds. research.py followed by refine.py reproduces the selected model; recover_parameters.py re-estimates equation coefficients. evaluation_seasons.csv gives season and symbol PnL, while research_results.json retains the detailed tables and rejected benchmark. The included offline tests passed; live exchange behavior was not tested.

## Source and scope

Primary sources supplied by the user: [External] CTC 2026 Case Packet.pdf (17 pages), and hurricane-season-research.zip: case.json, the 600 season feeds, market.csv files, settlement.json files, history/manifest.json and starter_kit. Rules, data fields and API paths come from these files; fitted equations, credibility rates, model outputs and PnL estimates are this analysis. No private simulator source or future live-season path was available.

The research ZIP contains only historical house consensus prices, not executable books. It contains no announcements. The delivered package therefore has a real valuation model and a testable trading hypothesis, with a remaining exchange-integration step before execution.
