from __future__ import annotations

import unittest
from datetime import UTC, datetime

from scripts.evaluate import parse_calls


def message(message_id: int, text: str) -> dict[str, object]:
    return {
        "message_id": message_id,
        "channel_id": 100,
        "channel_handle": "testchannel",
        "author_id": 200,
        "author_name": "tester",
        "sent_at_utc": datetime(2026, 6, 1, tzinfo=UTC).isoformat(),
        "text_raw": text,
    }


class SignalTrailParserGuardTests(unittest.TestCase):
    def test_corporate_acquisition_news_is_not_a_buy_call(self) -> None:
        calls = parse_calls(
            [
                message(
                    1,
                    "AVIVA signs agreement to acquire the remaining 26% stake in Aviva Life Insurance Company India. "
                    "To buy the stake from Dabur Invest Corp and acquire full ownership.",
                )
            ],
            {"DABUR": "DABUR.NS"},
        )

        self.assertEqual(calls, [])

    def test_quarterly_results_commentary_is_not_a_futures_call(self) -> None:
        calls = parse_calls(
            [
                message(
                    1,
                    "TCS Q1FY27 Results: Total Contract Value rose; LTM attrition improved; "
                    "long-term future growth commentary remains positive.",
                )
            ],
            {"TCS": "TCS.NS", "TOTAL": "TOTAL.NS", "LTM": "LTM.NS"},
        )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
