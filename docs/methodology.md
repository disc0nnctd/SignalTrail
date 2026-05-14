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

## Evaluation Methods

### Target/Stop Simulation

If entry/target/stop are coherent:

- win: target before stop
- loss: stop before target
- flat: neither in horizon

If target and stop are hit in same daily candle, default is conservative `stop_first`.

### Directional Horizon Fallback

When full trade-plan levels are unavailable:

- evaluate signed return over fixed horizons
- compare directional performance vs benchmark

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

- fewer than 20 calls: `IS` (insufficient sample)
- 20–49: provisional
- 50+: full ranking eligible

## Limitations

- historical performance is not predictive
- message edits/deletes can affect evidence completeness
- parser ambiguity can create false positives/negatives
- private follow-up instructions cannot be inferred from public posts
