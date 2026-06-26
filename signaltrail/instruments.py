from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


INDEX_YAHOO_SYMBOLS: dict[str, str] = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
}
NON_UNDERLYING_TOKENS = {"BUY", "SELL", "LONG", "SHORT", "ADD", "ABOVE", "BELOW", "ENTRY"}

OPTION_PATTERNS = (
    re.compile(
        r"\b(?P<underlying>NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|[A-Z]{2,20})?\s*"
        r"(?P<strike>\d{4,6})\s*(?P<option_type>CE|PE)\b",
        re.I,
    ),
    re.compile(
        r"\b(?P<underlying>NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|[A-Z]{2,20})?\s*"
        r"(?P<option_type>CE|PE)\s*(?P<strike>\d{4,6})\b",
        re.I,
    ),
)
TARGET_PAT = re.compile(r"\b(?:tgt|target)\s*\d*\s*(?:=|:|at|-)?\s*(\d+(?:\.\d+)?)", re.I)
ENTRY_AT_PAT = re.compile(r"(?:@\s*|\sat\s+|entry\s*(?:=|:|at)?\s*)(\d+(?:\.\d+)?)", re.I)
TRAILING_STOP_PAT = re.compile(
    r"\b(?:sl\s*(?:at|to)\s*cost|cost\s*sl|trail(?:ing)?\s*sl|book\s*partial|hold\s+with\s+sl)\b",
    re.I,
)
CONTINUATION_PAT = re.compile(
    r"\b(?:add\s+more|add\b|re-?entry|reenter|again\s+entry|hold\b|trail(?:ing)?\s*sl|"
    r"sl\s*(?:at|to)\s*cost|target\s+(?:hit|done|aa\s*gaya|aagaya)|tgt\s+(?:hit|done))\b",
    re.I,
)
HINGLISH_BUY_PAT = re.compile(r"\b(?:kharid(?:o|na|na hai)?|le\s*lo|lena|buy\s*karo|entry\s*lo)\b", re.I)
HINGLISH_SELL_PAT = re.compile(r"\b(?:bech(?:o|na)?|sell\s*karo|short\s*karo|nikal\s*jao|exit\s*karo)\b", re.I)
HINGLISH_WAIT_PAT = re.compile(r"\b(?:ruk(?:o|na)?|intezar|wait\s*karo|kal\s*tak\s*hold|hold\s*karo)\b", re.I)


@dataclass
class InstrumentInfo:
    instrument_type: str
    symbol: str | None = None
    display_symbol: str | None = None
    underlying: str | None = None
    strike: float | None = None
    option_type: str | None = None
    expiry: str | None = None
    target_hints: list[float] = field(default_factory=list)
    entry_hint: float | None = None
    is_continuation: bool = False
    parent_key: str | None = None
    trailing_stop_rule: str | None = None
    notes: list[str] = field(default_factory=list)


def normalize_hinglish_intent(text: str) -> tuple[bool, bool, bool]:
    return (
        bool(HINGLISH_BUY_PAT.search(text)),
        bool(HINGLISH_SELL_PAT.search(text)),
        bool(HINGLISH_WAIT_PAT.search(text)),
    )


def extract_target_hints(text: str, direction: str) -> list[float]:
    values: list[float] = []
    for match in TARGET_PAT.finditer(text):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in values:
            values.append(value)
    if direction == "bearish":
        values.sort(reverse=True)
    else:
        values.sort()
    return values


def extract_entry_hint(text: str) -> float | None:
    match = ENTRY_AT_PAT.search(f" {text} ")
    if not match:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def detect_instrument(
    text: str,
    symbols: list[str],
    symbol_map: dict[str, str],
    direction: str,
) -> InstrumentInfo:
    upper = text.upper()
    targets = extract_target_hints(text, direction)
    entry_hint = extract_entry_hint(text)
    continuation = bool(CONTINUATION_PAT.search(text))
    trailing_rule = "sl_at_cost_or_trailing_after_target" if TRAILING_STOP_PAT.search(text) else None

    for pattern in OPTION_PATTERNS:
        match = pattern.search(upper)
        if not match:
            continue
        option_type = match.group("option_type").upper()
        strike = float(match.group("strike"))
        raw_underlying = (match.group("underlying") or "").upper().strip()
        if raw_underlying in NON_UNDERLYING_TOKENS:
            raw_underlying = ""
        underlying = raw_underlying or _nearest_index_name(upper) or (symbols[0].split(".", 1)[0] if symbols else None)
        symbol = f"{underlying} {int(strike)} {option_type}" if underlying else f"{int(strike)} {option_type}"
        return InstrumentInfo(
            instrument_type="options",
            symbol=symbol,
            display_symbol=symbol,
            underlying=underlying,
            strike=strike,
            option_type=option_type,
            target_hints=targets,
            entry_hint=entry_hint,
            is_continuation=continuation,
            parent_key=_parent_key("options", underlying or symbol, direction),
            trailing_stop_rule=trailing_rule,
            notes=["options_premium_required"],
        )

    index_name = _nearest_index_name(upper)
    first_symbol_base = symbols[0].split(".", 1)[0].upper() if symbols else ""
    if index_name and (not symbols or symbols[0] in set(INDEX_YAHOO_SYMBOLS.values()) or first_symbol_base == index_name):
        mapped = INDEX_YAHOO_SYMBOLS.get(index_name)
        return InstrumentInfo(
            instrument_type="index",
            symbol=mapped or index_name,
            display_symbol=index_name,
            underlying=index_name,
            target_hints=targets,
            entry_hint=entry_hint,
            is_continuation=continuation,
            parent_key=_parent_key("index", index_name, direction),
            trailing_stop_rule=trailing_rule,
            notes=[] if mapped else ["index_market_symbol_unmapped"],
        )

    symbol = symbols[0] if symbols else None
    base = symbol.split(".", 1)[0] if symbol else None
    instrument_type = "futures" if re.search(r"\b(?:FUT|FUTURE|FUTURES)\b", upper) else "equity"
    return InstrumentInfo(
        instrument_type=instrument_type,
        symbol=symbol,
        display_symbol=base or symbol,
        underlying=base,
        target_hints=targets,
        entry_hint=entry_hint,
        is_continuation=continuation,
        parent_key=_parent_key(instrument_type, base or symbol, direction),
        trailing_stop_rule=trailing_rule,
    )


def _nearest_index_name(text: str) -> str | None:
    for name in ("BANKNIFTY", "NIFTYBANK", "NIFTY50", "NIFTY", "FINNIFTY", "MIDCPNIFTY"):
        if re.search(rf"\b{name}\b", text):
            return "BANKNIFTY" if name == "NIFTYBANK" else name
    return None


def _parent_key(instrument_type: str, symbol: str | None, direction: str) -> str | None:
    if not symbol:
        return None
    return f"{instrument_type}:{symbol}:{direction}"


def instrument_options_details(info: InstrumentInfo) -> dict[str, Any] | None:
    if info.instrument_type != "options":
        return None
    return {
        "underlying": info.underlying,
        "strike": info.strike,
        "option_type": info.option_type,
        "expiry": info.expiry,
        "premium_data_status": "unavailable",
    }
