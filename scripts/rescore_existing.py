#!/usr/bin/env python3
"""Re-parse existing messages.json with the current parser and rescore.

Reuses the candle cache so no network fetch is needed. Used to validate the
parser fix and regenerate outputs without a full Telegram fetch.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from signaltrail.market_data import Candle, fetch_candles
from scripts.evaluate import (
    SCORABLE_INTENTS,
    excluded_outcome,
    outcome_for_call,
    parse_calls,
    pre_market_exclusion_reason,
    score_bucket,
)


def main() -> int:
    out_dir = Path("data/output")
    messages = json.loads((out_dir / "messages.json").read_text(encoding="utf-8"))
    print(f"Loaded {len(messages)} messages from existing messages.json")

    cfg = json.loads(Path("channels.json").read_text(encoding="utf-8"))
    universe = cfg.get("universe") or []
    symbol_map: dict[str, str] = {}
    for asset in universe:
        sym = str(asset.get("symbol") or "").upper().strip()
        if not sym:
            continue
        base = sym.split(".", 1)[0]
        symbol_map[base] = sym
        symbol_map[sym] = sym

    parsed = parse_calls(messages, symbol_map)
    print(f"Parsed {len(parsed)} calls with FIXED parser")

    horizons = [1, 3, 5, 10]
    now = datetime.now(UTC)

    bench_candles: list[Candle] | None = None
    try:
        bench_candles = fetch_candles("NIFTYBEES.NS", "2y", "1d")
    except Exception as exc:
        print(f"Benchmark fetch failed: {exc}")

    candle_cache: dict[str, list[Candle]] = {}
    outcomes = []
    for call in parsed:
        try:
            sent = datetime.fromisoformat(call.sent_at_utc)
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=UTC)
        except ValueError:
            continue
        exclusion_reason = pre_market_exclusion_reason(call)
        if exclusion_reason:
            call_outcomes = [excluded_outcome(exclusion_reason)]
        else:
            if call.intent not in SCORABLE_INTENTS:
                continue
            if call.symbol not in candle_cache:
                try:
                    candle_cache[call.symbol] = fetch_candles(call.symbol, "2y", "1d")
                except Exception:
                    continue
            call_outcomes = outcome_for_call(
                symbol=call.symbol,
                direction=call.direction,
                sent_at=sent,
                horizons=horizons,
                symbol_candles=candle_cache[call.symbol],
                benchmark_candles=bench_candles,
                win_thresh_pct=0.01,
                loss_thresh_pct=-0.01,
                intent=call.intent,
                entry_hint=call.entry_hint,
                stop_hint=call.stop_hint,
                target_hint=call.target_hint,
                target_hints=call.target_hints,
                trigger_above=call.trigger_above,
                trigger_below=call.trigger_below,
                prefer_target_stop=True,
                same_bar_policy="stop_first",
            )
        for row in call_outcomes:
            outcomes.append({
                "call_id": call.call_id,
                "message_id": call.message_id,
                "channel_id": call.channel_id,
                "channel_handle": call.channel_handle,
                "author_id": call.author_id,
                "author_name": call.author_name,
                "sent_at_utc": call.sent_at_utc,
                "symbol": call.symbol,
                "display_symbol": call.display_symbol,
                "instrument_type": call.instrument_type,
                "underlying": call.underlying,
                "options_details": call.options_details,
                "direction": call.direction,
                "intent": call.intent,
                "call_type": call.call_type,
                "parser_confidence": call.parser_confidence,
                "entry_hint": call.entry_hint,
                "stop_hint": call.stop_hint,
                "target_hint": call.target_hint,
                "target_hints": call.target_hints,
                "trigger_above": call.trigger_above,
                "trigger_below": call.trigger_below,
                "is_continuation": call.is_continuation,
                "parent_call_id": call.parent_call_id,
                "trailing_stop_rule": call.trailing_stop_rule,
                "text": call.text,
                "verifier_verdict": call.verifier_verdict,
                "verifier_reason": call.verifier_reason,
                **row,
            })

    print(f"Computed {len(outcomes)} outcome rows")

    # Score per channel
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for r in outcomes:
        by_channel[r["channel_handle"]].append(r)

    channel_scores = {}
    for handle, recs in by_channel.items():
        channel_scores[handle] = score_bucket(recs, now, 30.0)

    # Write outputs
    (out_dir / "outcomes.json").write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
    (out_dir / "parsed_calls.json").write_text(json.dumps([c.__dict__ for c in parsed], indent=2), encoding="utf-8")

    scores_payload = {
        "generated_at_utc": now.isoformat(),
        "scoring_policy": "author_first_channel_only_if_single_poster",
        "channel_scores": {k: {"score": v["recency_weighted_score"], "tier": v["tier"], "calls_count": v["calls_count"]} for k, v in channel_scores.items()},
    }
    (out_dir / "scores.json").write_text(json.dumps(scores_payload, indent=2), encoding="utf-8")
    print(f"Wrote scores.json with {len(channel_scores)} channels")
    print(json.dumps(scores_payload["channel_scores"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
