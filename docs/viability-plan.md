# SignalTrail Viability Plan

## Current Standing

SignalTrail is a working prototype for public Telegram signal transparency. It can fetch configured Telegram sources through the local MTProto session, parse messages, score calls against market candles, and publish a masked static leaderboard.

It is not ready for trusted public ranking claims until extraction precision and data coverage improve. The July 2026 live smoke probe proved Telegram access and end-to-end output writing, but also exposed parser false positives from corporate news and quarterly-results commentary.

## Release Criteria

1. Telegram access smoke test reports reachable source count, error count, and recent message count without exposing raw messages.
2. Parser precision is measured against a labeled fixture set for trade calls, news, earnings, options, continuations, Hinglish, and wait/watch messages.
3. Public leaderboard includes only rows above the configured insufficient-sample threshold.
4. Options calls are either scored from premium candles or explicitly excluded from ranked metrics.
5. The dashboard loads the generated JSON without fallback data and exposes methodology, sample-size policy, and correction policy.

## Near-Term Fix Plan

1. Harden rule extraction against non-signal news text.
2. Keep `evaluate.py` as the canonical public JSON writer and make the legacy importer opt-in only.
3. Add regression tests for every false-positive pattern found in live probes.
4. Build a cached-message integration harness that replays Telegram messages without hitting Telegram.
5. Add deterministic candle fixtures for scoring tests so target/stop and directional outcomes can be verified without live market data.

## Medium-Term Improvements

1. Split `scripts/evaluate.py` into ingestion, parsing, scoring, metrics, and publication modules.
2. Add a source-quality report that shows per-channel fetch success, parsed-call density, excluded-row reasons, and sample eligibility.
3. Wire the optional LLM verifier into a measurable review mode with before/after precision metrics.
4. Add options premium data support or keep options excluded from performance metrics by policy.
5. Add browser QA for the static dashboard at desktop and mobile widths before every public update.

## Operating Policy

- Treat public leaderboard rows as experimental until a source has enough resolved calls for the configured threshold.
- Do not publish raw Telegram messages, private chat data, handles, phone numbers, emails, or unmasked author identities.
- Keep generated caches out of git; commit only the static public artifact intended for deployment.
