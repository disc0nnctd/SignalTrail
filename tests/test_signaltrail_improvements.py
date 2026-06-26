from __future__ import annotations

import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from signaltrail.market_data import Candle
from scripts import evaluate
from scripts.evaluate import (
    SCORABLE_INTENTS,
    compute_metrics_v2,
    excluded_outcome,
    outcome_for_call,
    parse_calls,
    pre_market_exclusion_reason,
    score_bucket,
    _write_leaderboard,
)


def message(message_id: int, text: str, sent_at: datetime | None = None) -> dict[str, object]:
    return {
        "message_id": message_id,
        "channel_id": 100,
        "channel_handle": "testchannel",
        "author_id": 200,
        "author_name": "tester",
        "sent_at_utc": (sent_at or datetime(2026, 6, 1, tzinfo=UTC)).isoformat(),
        "text_raw": text,
    }


class SignalTrailImprovementTests(unittest.TestCase):
    def test_options_parse_as_premium_instrument_with_multiple_targets(self) -> None:
        calls = parse_calls(
            [message(1, "BUY NIFTY 23000 CE @ 150 TGT1 300 TGT2 420 SL 80")],
            {},
        )

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.instrument_type, "options")
        self.assertEqual(call.options_details["underlying"], "NIFTY")
        self.assertEqual(call.options_details["option_type"], "CE")
        self.assertEqual(call.entry_hint, 150)
        self.assertEqual(call.stop_hint, 80)
        self.assertEqual(call.target_hints, [300, 420])

    def test_options_underlying_does_not_use_trade_verb(self) -> None:
        calls = parse_calls([message(1, "BUY 23000 CE @ 150 TGT 300 SL 80")], {})

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].instrument_type, "options")
        self.assertNotEqual(calls[0].options_details["underlying"], "BUY")

    def test_index_call_uses_yahoo_symbol_and_display_symbol(self) -> None:
        calls = parse_calls([message(1, "BUY NIFTY above 23500 target 23800 sl 23300")], {})

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].instrument_type, "index")
        self.assertEqual(calls[0].symbol, "^NSEI")
        self.assertEqual(calls[0].display_symbol, "NIFTY")

    def test_unmapped_index_stays_explicit_for_exclusion(self) -> None:
        calls = parse_calls([message(1, "BUY FINNIFTY above 21500 target 21800 sl 21300")], {})

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].instrument_type, "index")
        self.assertEqual(calls[0].symbol, "FINNIFTY")
        self.assertEqual(calls[0].display_symbol, "FINNIFTY")

    def test_hinglish_buy_phrase_is_actionable(self) -> None:
        calls = parse_calls(
            [message(1, "RELIANCE le lo @ 100 target 110 sl 95")],
            {"RELIANCE": "RELIANCE.NS"},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].direction, "bullish")
        self.assertEqual(calls[0].intent, "buy_now")

    def test_add_more_links_to_parent_and_marks_continuation(self) -> None:
        calls = parse_calls(
            [
                message(1, "BUY RELIANCE @ 100 target 110 sl 95"),
                message(2, "Add more RELIANCE at 98 dip target 110 sl 95"),
            ],
            {"RELIANCE": "RELIANCE.NS"},
        )

        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0].is_continuation)
        self.assertTrue(calls[1].is_continuation)
        self.assertEqual(calls[1].parent_call_id, calls[0].call_id)

    def test_hold_update_links_to_same_source_parent_only(self) -> None:
        calls = parse_calls(
            [
                message(1, "BUY RELIANCE @ 100 target 110 sl 95", sent_at=datetime(2026, 6, 1, tzinfo=UTC)),
                {**message(2, "Hold RELIANCE with SL at cost", sent_at=datetime(2026, 6, 2, tzinfo=UTC)), "author_id": 201},
                message(3, "Hold RELIANCE with SL at cost", sent_at=datetime(2026, 6, 3, tzinfo=UTC)),
            ],
            {"RELIANCE": "RELIANCE.NS"},
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].intent, "continuation_update")
        self.assertEqual(calls[1].parent_call_id, calls[0].call_id)
        self.assertEqual(calls[1].author_id, calls[0].author_id)

    def test_continuation_update_reaches_excluded_outcome_gate(self) -> None:
        calls = parse_calls(
            [
                message(1, "BUY RELIANCE @ 100 target 110 sl 95", sent_at=datetime(2026, 6, 1, tzinfo=UTC)),
                message(2, "Hold RELIANCE with SL at cost", sent_at=datetime(2026, 6, 2, tzinfo=UTC)),
            ],
            {"RELIANCE": "RELIANCE.NS"},
        )

        update = calls[1]
        reason = pre_market_exclusion_reason(update)
        self.assertEqual(update.intent, "continuation_update")
        self.assertNotIn(update.intent, SCORABLE_INTENTS)
        self.assertEqual(reason, "continuation_update")
        self.assertEqual(excluded_outcome(reason)["evaluation_method"], "continuation_update")

    def test_excluded_rows_do_not_count_as_scored_calls(self) -> None:
        rows = [
            {"call_id": "scored", "label": "win", "outcome_credit": 1.0, "net_return_pct": 2, "benchmark_excess_return_pct": 2, "parser_confidence": 0.9, "sent_at_utc": datetime(2026, 6, 1, tzinfo=UTC).isoformat()},
            {"call_id": "option", **excluded_outcome("options_no_premium_data")},
            {"call_id": "update", **excluded_outcome("continuation_update")},
        ]

        metrics = compute_metrics_v2(rows)
        score = score_bucket(rows, datetime(2026, 6, 25, tzinfo=UTC), 30)

        self.assertEqual(metrics["counts"]["call_count"], 1)
        self.assertEqual(metrics["counts"]["excluded_row_count"], 2)
        self.assertEqual(score["calls_count"], 1)

    def test_public_leaderboard_redacts_message_examples(self) -> None:
        now = datetime(2026, 6, 25, tzinfo=UTC)
        outcomes = [
            {
                "call_id": "c1",
                "message_id": 1,
                "channel_id": 10,
                "channel_handle": "chan",
                "author_id": 20,
                "author_name": "ann",
                "sent_at_utc": now.isoformat(),
                "symbol": "RELIANCE.NS",
                "display_symbol": "RELIANCE",
                "instrument_type": "equity",
                "direction": "bullish",
                "intent": "buy_now",
                "call_type": "target",
                "parser_confidence": 0.9,
                "evaluation_window": "1d",
                "evaluation_method": "directional_horizon",
                "exit_reason": "horizon",
                "net_return_pct": 2,
                "benchmark_excess_return_pct": 2,
                "label": "win",
                "outcome_credit": 1.0,
                "text": "BUY RELIANCE @ 100 target 110 sl 95 contact @secret https://t.me/secret",
            }
        ]
        metrics = compute_metrics_v2(outcomes, parsed_calls_count=1, message_count=1)
        payload = {
            "generated_at_utc": now.isoformat(),
            "message_count": 1,
            "parsed_calls_count": 1,
            "outcome_rows_count": 1,
            "benchmark_symbol": "NIFTYBEES.NS",
            "market_data_range": "1y",
            "lookback_days": 30,
            "horizons_days": [1],
            "prefer_target_stop": True,
            "same_bar_policy": "stop_first",
            "metrics_v2": metrics,
            "rankings": {"author_rankings": [{"key": "20", **score_bucket(outcomes, now, 30)}]},
        }

        with TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "leaderboard.json"
            _write_leaderboard(payload, outcomes, {"10": "chan"}, out_path, now, is_threshold=1)
            data = json.loads(out_path.read_text())
            call = next(iter(data["drilldowns"].values()))["parsed_calls"][0]
            sample = data["breakdown_samples"][0]

        self.assertNotIn("raw_message", call)
        self.assertNotIn("raw_message", sample)
        self.assertIn("[handle]", call["message_excerpt"])
        self.assertIn("[link]", call["message_excerpt"])

    def test_llm_extracted_targets_survive_redetection(self) -> None:
        original = evaluate.llm_extract_call_openai_compatible
        try:
            evaluate.llm_extract_call_openai_compatible = lambda **_: {
                "symbol": "RELIANCE",
                "direction": "bullish",
                "entry": 100,
                "stop_loss": 95,
                "target": 110,
                "targets": [110, 120],
                "confidence": 0.9,
            }
            calls = parse_calls(
                [message(1, "BUY RELIANCE breakout", sent_at=datetime(2026, 6, 1, tzinfo=UTC))],
                {"RELIANCE": "RELIANCE.NS"},
                llm_extract_enabled=True,
                llm_extract_api_key="test-key",
            )
        finally:
            evaluate.llm_extract_call_openai_compatible = original

        self.assertEqual(calls[0].target_hints, [110, 120])

    def test_malformed_llm_response_falls_back_to_regex_parse(self) -> None:
        original = evaluate.llm_extract_call_openai_compatible
        attempted = [False]
        def raise_bad_response(**_: object) -> dict[str, object]:
            attempted[0] = True
            raise IndexError("bad response")
        try:
            evaluate.llm_extract_call_openai_compatible = raise_bad_response
            calls = parse_calls(
                [message(1, "BUY RELIANCE breakout", sent_at=datetime(2026, 6, 1, tzinfo=UTC))],
                {"RELIANCE": "RELIANCE.NS"},
                llm_extract_enabled=True,
                llm_extract_api_key="test-key",
            )
        finally:
            evaluate.llm_extract_call_openai_compatible = original

        self.assertTrue(attempted[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].symbol, "RELIANCE.NS")

    def test_multi_target_partial_win_gets_fractional_credit(self) -> None:
        start = datetime(2026, 6, 1, tzinfo=UTC)
        candles = [
            Candle(start + timedelta(days=0), 100, 104, 99, 102, 1000),
            Candle(start + timedelta(days=1), 102, 111, 101, 108, 1000),
            Candle(start + timedelta(days=2), 108, 112, 104, 106, 1000),
            Candle(start + timedelta(days=3), 106, 109, 105, 107, 1000),
        ]

        rows = outcome_for_call(
            symbol="RELIANCE.NS",
            direction="bullish",
            sent_at=start,
            horizons=[3],
            symbol_candles=candles,
            benchmark_candles=None,
            win_thresh_pct=0.01,
            loss_thresh_pct=-0.01,
            intent="buy_now",
            entry_hint=100,
            stop_hint=95,
            target_hint=115,
            target_hints=[110, 115],
        )

        self.assertEqual(rows[0]["label"], "partial_win")
        self.assertEqual(rows[0]["targets_hit_count"], 1)
        self.assertEqual(rows[0]["target_count"], 2)
        self.assertEqual(rows[0]["outcome_credit"], 0.5)

        metrics = compute_metrics_v2([{**rows[0], "call_id": "c1"}])
        self.assertEqual(metrics["call_level"]["resolved_win_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
