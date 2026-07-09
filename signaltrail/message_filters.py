from __future__ import annotations

import re
from typing import Final

NEWS_CONTEXT_PAT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    r"results?|q[1-4](?:fy)?\d{0,2}|earnings|revenue|profit|margin|dividend|attrition|"
    r"contract\s+value|commentary|agreement|acquir(?:e|es|ed|ing)|acquisition|stake|merger|"
    r"partnership|deal|company|corp(?:oration)?"
    r")\b",
    re.I,
)
TRADE_STRUCTURE_PAT: Final[re.Pattern[str]] = re.compile(
    r"(?:@\s*\d+|\b(?:entry|target|tgt|sl|stop|stoploss|stop\s+loss|above|below|cmp)\b)",
    re.I,
)


def has_direct_trade_command(text: str, symbols: list[str]) -> bool:
    upper = text.upper()
    for symbol in symbols:
        base = re.escape(symbol.split(".", 1)[0].upper())
        if re.search(rf"\b(?:BUY|SELL|LONG|SHORT)\s+{base}\b", upper):
            return True
        if re.search(rf"\b{base}\s+(?:BUY|SELL|LONG|SHORT)\b", upper):
            return True
    return False


def has_trade_structure(text: str, symbols: list[str]) -> bool:
    return bool(TRADE_STRUCTURE_PAT.search(text)) or has_direct_trade_command(text, symbols)


def is_non_actionable_news_context(text: str, symbols: list[str]) -> bool:
    return bool(NEWS_CONTEXT_PAT.search(text)) and not has_trade_structure(text, symbols)
