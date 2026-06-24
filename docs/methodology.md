# Methodology

## Scope

SignalTrail evaluates historical outcomes of public Telegram market calls.

This is a retrospective scoring system, not a live trading signal engine.

## Call Categories

1. Trade-plan calls: entry/trigger + target + stop
2. Partial-plan calls: directional with incomplete levels
3. Directional calls: bullish/bearish without full trade plan
4. Non-actionable messages: commentary/noise/education

Only actionable calls are scored.

## What Counts as a Resolved Call

A call is **resolved** when one of the following conditions is met within the evaluation horizon:

- **Target hit** — the daily high touches or exceeds the target level (long) or the daily low touches or goes below the target (short)
- **Stop hit** — the daily low touches or goes below the stop level (long) or the daily high touches or exceeds the stop (short)
- **Horizon expired** — neither target nor stop is hit by the last day in the horizon; call is scored on final signed return vs benchmark

Calls where the parser found only a direction (no levels) are always resolved at horizon using directional return.

Calls with coherent levels where neither target nor stop is reached within horizon are scored as `flat` (not a win or loss).

## Benchmark

The default benchmark is **NIFTYBEES.NS** (Nifty BeES ETF on NSE), used as a proxy for broad Indian equity market returns. All directional horizon scores are computed as *excess return* = call return − benchmark return over the same period.

The benchmark can be changed with `--benchmark-symbol` (e.g. `GOLDBEES.NS` for gold).

## Evaluation Methods

### Target/Stop Simulation

If entry/target/stop are coherent:

- win: target before stop
- loss: stop before target
- flat: neither in horizon

If target and stop are hit in the same daily candle, the default is the conservative outcome `stop_first`.

### Directional Horizon Fallback

When full trade-plan levels are unavailable:

- evaluate signed return over fixed horizons (default: 1, 3, 5, 10 trading days)
- compare directional performance vs benchmark excess return

## Reported Metrics

- call-level win rate
- resolved-only win rate
- row-level win rate
- target/stop win rate
- benchmark-relative directional win rate
- profit factor
- trade-plan coverage
- duplicate/row density indicators

## Confidence and Sample Size

- fewer than N calls: `IS` (insufficient sample) — see note below
- 20–49: provisional
- 50+: full ranking eligible

> **Note on IS threshold:** `evaluate.py` uses `--is-threshold` (default **8**) to control when a
> channel is marked `IS`. The legacy `scripts/import-telegram-quality.py` uses `--min-public-calls`
> (default **20**). When deploying, use a consistent threshold across both scripts. The recommended
> default for a public-facing leaderboard is **20** to ensure meaningful sample sizes.

## Limitations

- historical performance is not predictive
- message edits/deletes can affect evidence completeness
- parser ambiguity can create false positives/negatives
- private follow-up instructions cannot be inferred from public posts
