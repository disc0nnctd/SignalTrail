#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
from urllib.error import URLError
from urllib.request import Request, urlopen
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Tuple

from signaltrail.instruments import (
    detect_instrument,
    instrument_options_details,
    normalize_hinglish_intent,
)
from signaltrail.market_data import Candle, fetch_candles
from signaltrail.message_filters import is_non_actionable_news_context


BUY_TOKENS = ("buy", "long", "accumulate", "breakout")
SELL_TOKENS = ("sell", "short", "exit", "avoid")
WAIT_TOKENS = (
    "wait",
    "watch",
    "don't buy",
    "do not buy",
    "not buy",
    "avoid buy",
    "only above",
    "only below",
)
SCORABLE_INTENTS = {"buy_now", "sell_now", "conditional_buy", "conditional_sell"}
CONDITIONAL_TOKENS = ("above", "below", "if closes above", "if closes below", "cmp")

ENTRY_PAT = re.compile(r"\b(?:buy|long|sell|short)\s+[A-Z0-9.&-]+\s*@\s*(\d+(?:\.\d+)?)", re.I)
STOP_PAT = re.compile(r"\b(?:sl|stop|stoploss|stop loss)\s*(?:below|at|=|:)?\s*(\d+(?:\.\d+)?)", re.I)
TGT_PAT = re.compile(r"\b(?:tgt|target)\s*(?:=|:|at)?\s*(\d+(?:\.\d+)?)", re.I)
ABOVE_PAT = re.compile(r"\babove\s+(\d+(?:\.\d+)?)", re.I)
BELOW_PAT = re.compile(r"\bbelow\s+(\d+(?:\.\d+)?)", re.I)
TOKEN_PAT = re.compile(r"\b[A-Z]{2,20}\b")

# Word-boundary intent patterns. The bare-substring checks they replace matched
# 'along'/'belong' as long (bullish) and 'exiting'/'shortage' as sell tokens,
# turning news headlines into spurious trade calls. Genuine inflections (buying,
# selling, exits) are kept; the gerund 'exiting' is rejected because it is the
# form most often used in non-actionable news ('BlackRock exiting investment').
BUY_INTENT_PAT = re.compile(r"\b(?:buy(?:ing|s)?|accumulate(?:s|d)?|breakouts?|add\s+(?:more|at|on|near))\b", re.I)
LONG_INTENT_PAT = re.compile(r"\b(?:go\s+long|long\s+(?:[A-Z0-9.&-]{2,20}|above|below|at|near|@))\b", re.I)
SELL_INTENT_PAT = re.compile(r"\b(?:sell(?:ing|s|er|ers)?|exit(?:s|ed)?|avoid(?:s|ed)?)\b", re.I)
SHORT_INTENT_PAT = re.compile(r"\b(?:go\s+short|short\s+(?:[A-Z0-9.&-]{2,20}|above|below|at|near|@))\b", re.I)
WAIT_WORD_PAT = re.compile(r"\b(?:wait(?:ing|s)?|watch(?:es|ing)?)\b", re.I)


@dataclass
class ParsedCall:
    call_id: str
    message_id: int
    channel_id: int
    channel_handle: str
    author_id: int | None
    author_name: str
    sent_at_utc: str
    symbol: str
    display_symbol: str
    instrument_type: str
    underlying: str | None
    options_details: Dict[str, Any] | None
    direction: str
    call_type: str
    intent: str
    parser_confidence: float
    entry_hint: float | None
    stop_hint: float | None
    target_hint: float | None
    target_hints: List[float]
    trigger_above: float | None
    trigger_below: float | None
    is_continuation: bool
    parent_call_id: str | None
    trailing_stop_rule: str | None
    text: str
    verifier_verdict: str
    verifier_reason: str


def load_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_num(pat: re.Pattern[str], text: str) -> float | None:
    m = pat.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def classify_intent(text: str) -> Tuple[str, str, float]:
    lowered = text.lower()
    has_buy = bool(BUY_INTENT_PAT.search(text) or LONG_INTENT_PAT.search(text))
    has_sell = bool(SELL_INTENT_PAT.search(text) or SHORT_INTENT_PAT.search(text))
    hinglish_buy, hinglish_sell, hinglish_wait = normalize_hinglish_intent(text)
    has_buy = has_buy or hinglish_buy
    has_sell = has_sell or hinglish_sell
    # Multi-word wait/negation phrases still need substring matching.
    has_wait = any(t in lowered for t in WAIT_TOKENS) or bool(WAIT_WORD_PAT.search(text)) or hinglish_wait
    conditional = any(t in lowered for t in CONDITIONAL_TOKENS)

    if has_wait and not has_buy and not has_sell:
        return "wait", "neutral", 0.75
    if has_wait and has_buy and not has_sell:
        return "wait", "bullish", 0.8
    if has_wait and has_sell and not has_buy:
        return "wait", "bearish", 0.8
    if has_buy and not has_sell:
        return ("conditional_buy" if conditional else "buy_now"), "bullish", (0.82 if conditional else 0.9)
    if has_sell and not has_buy:
        return ("conditional_sell" if conditional else "sell_now"), "bearish", (0.82 if conditional else 0.9)
    if has_buy and has_sell:
        return "ambiguous", "neutral", 0.35
    return "noise", "neutral", 0.2


def verify_message_intent(text: str, intent: str, direction: str) -> Tuple[str, str]:
    lowered = text.lower()
    if intent in {"noise", "ambiguous"}:
        return "reject", "intent_noise_or_ambiguous"
    if "don't buy" in lowered or "do not buy" in lowered or "not buy" in lowered:
        if intent in {"buy_now", "conditional_buy"}:
            return "reject", "negated_buy_phrase"
    if WAIT_WORD_PAT.search(text) and intent in {"buy_now", "sell_now"}:
        return "reject", "wait_phrase_conflict"
    if intent in {"conditional_buy", "conditional_sell"} and not ("above" in lowered or "below" in lowered):
        return "review", "conditional_without_explicit_trigger"
    if direction not in {"bullish", "bearish"}:
        return "reject", "invalid_direction"
    return "accept", "ok"


def llm_verify_message_ollama(
    text: str,
    intent: str,
    direction: str,
    endpoint: str,
    model: str,
    timeout_sec: int,
) -> Tuple[str, str]:
    prompt = (
        "You are a strict trading-message verifier.\n"
        "Classify message intent for cash-equity recommendation extraction.\n"
        "Return JSON only with keys: verdict, reason.\n"
        "verdict must be one of: accept, reject, review.\n"
        "reject when message is negated/deferred/noise/ambiguous for immediate directional call.\n"
        "review when partially parseable but conditional/unclear.\n"
        f"rule_intent={intent}, rule_direction={direction}\n"
        f"message={text}\n"
    )
    body = {"model": model, "prompt": prompt, "stream": False, "format": "json"}
    req = Request(
        endpoint.rstrip("/") + "/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError):
        return "review", "llm_unavailable"
    except json.JSONDecodeError:
        return "review", "llm_bad_response"

    raw = payload.get("response", "")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        parsed = {}
    verdict = str(parsed.get("verdict") or "").strip().lower()
    reason = str(parsed.get("reason") or "").strip().lower()[:120]
    if verdict not in {"accept", "reject", "review"}:
        return "review", "llm_invalid_verdict"
    return verdict, (reason or "llm_ok")


def llm_extract_call_openai_compatible(
    text: str,
    endpoint: str,
    model: str,
    api_key: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    system_prompt = (
        "Extract a structured Indian trading call from a Telegram message.\n"
        "Return JSON only with keys: symbol, direction, entry, stop_loss, target, targets, trigger_above, trigger_below, instrument_type, options_details, confidence, reason.\n"
        "direction must be bullish, bearish, or null.\n"
        "instrument_type must be equity, options, futures, index, or unknown. For CE/PE calls, entry/target/stop are option premium levels, not the underlying index.\n"
        "Do not invent levels that are not implied by the message.\n"
        "If uncertain, return nulls with low confidence.\n"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    req = Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {}
    try:
        parsed = json.loads(content) if isinstance(content, str) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def call_type(text: str) -> str:
    lowered = text.lower()
    if "breakout" in lowered:
        return "breakout"
    if "target" in lowered or "tgt" in lowered:
        return "target"
    if "result" in lowered or "q4" in lowered or "earnings" in lowered:
        return "news"
    return "opinion"


def symbol_candidates(text: str, symbol_map: Dict[str, str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for tok in TOKEN_PAT.findall(text.upper()):
        sym = symbol_map.get(tok)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_symbol(raw_symbol: Any, symbol_map: Dict[str, str]) -> str | None:
    if raw_symbol is None:
        return None
    raw = str(raw_symbol).upper().strip()
    if not raw:
        return None
    return symbol_map.get(raw) or symbol_map.get(raw.split(".", 1)[0])


def _validated_llm_levels(
    direction: str,
    entry: float | None,
    stop_loss: float | None,
    target: float | None,
) -> tuple[float | None, float | None, float | None]:
    if not _level_trade_ready(direction, entry, stop_loss, target):
        return None, None, None
    return entry, stop_loss, target


_OPTIONS_PAT = re.compile(r"\b\d{4,6}\s*(?:CE|PE)\b|\b(?:CE|PE)\s*\d{4,6}\b", re.I)
_INDEX_PAT = re.compile(r"\b(?:NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY)\b", re.I)


def _should_attempt_llm_extract(
    verifier_verdict: str,
    parser_confidence: float,
    symbols: List[str],
    entry_hint: float | None,
    stop_hint: float | None,
    target_hint: float | None,
    raw_text: str = "",
) -> bool:
    if verifier_verdict == "reject":
        return False
    # Always use LLM for options calls — regex can't distinguish premium vs underlying
    if _OPTIONS_PAT.search(raw_text):
        return True
    # Use LLM for index directional calls (NIFTY/BANKNIFTY without CE/PE)
    if _INDEX_PAT.search(raw_text) and not symbols:
        return True
    if not symbols:
        return True
    if parser_confidence < 0.85:
        return True
    return entry_hint is None or stop_hint is None or target_hint is None


def closest_idx_at_or_after(candles: List[Candle], ts: datetime) -> int | None:
    for i, c in enumerate(candles):
        if c.ts >= ts:
            return i
    return None


def _level_trade_ready(direction: str, entry: float | None, stop: float | None, target: float | None) -> bool:
    if entry is None or stop is None or target is None:
        return False
    if entry <= 0 or stop <= 0 or target <= 0:
        return False
    if direction == "bullish":
        return stop < entry < target
    if direction == "bearish":
        return target < entry < stop
    return False


def _find_triggered_entry(
    direction: str,
    intent: str,
    sent_idx: int,
    end_idx: int,
    candles: List[Candle],
    entry_hint: float | None,
    trigger_above: float | None,
    trigger_below: float | None,
) -> tuple[int | None, float | None, str]:
    # Conditional calls must actually trigger inside the horizon.
    if intent == "conditional_buy" and trigger_above is not None:
        for i in range(sent_idx, min(end_idx, len(candles) - 1) + 1):
            if candles[i].high >= trigger_above:
                return i, trigger_above, "trigger_above"
        return None, None, "not_triggered"
    if intent == "conditional_sell" and trigger_below is not None:
        for i in range(sent_idx, min(end_idx, len(candles) - 1) + 1):
            if candles[i].low <= trigger_below:
                return i, trigger_below, "trigger_below"
        return None, None, "not_triggered"

    # Immediate calls: prefer explicit @ entry, otherwise next/available close.
    if entry_hint is not None and entry_hint > 0:
        return sent_idx, entry_hint, "entry_hint"
    return sent_idx, candles[sent_idx].close, "next_close"


def _simulate_target_stop_outcome(
    direction: str,
    intent: str,
    sent_idx: int,
    horizon: int,
    candles: List[Candle],
    entry_hint: float | None,
    stop_hint: float | None,
    target_hint: float | None,
    target_hints: List[float] | None,
    trigger_above: float | None,
    trigger_below: float | None,
    win_thresh_pct: float,
    loss_thresh_pct: float,
    same_bar_policy: str = "stop_first",
) -> Dict[str, Any] | None:
    end_idx = sent_idx + horizon
    if end_idx >= len(candles):
        return None
    entry_idx, entry, entry_source = _find_triggered_entry(
        direction, intent, sent_idx, end_idx, candles, entry_hint, trigger_above, trigger_below
    )
    if entry_idx is None or entry is None:
        return {
            "evaluation_method": "target_stop",
            "entry_source": entry_source,
            "entry_price": None,
            "exit_price": None,
            "exit_reason": "not_triggered",
            "net_return_pct": 0.0,
            "benchmark_excess_return_pct": 0.0,
            "r_multiple": 0.0,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
            "label": "flat",
        }
    targets = [float(x) for x in (target_hints or []) if x and x > 0]
    if not targets and target_hint is not None:
        targets = [float(target_hint)]
    if direction == "bearish":
        targets = sorted({x for x in targets if x < entry}, reverse=True)
    else:
        targets = sorted({x for x in targets if x > entry})
    primary_target = targets[-1] if direction == "bullish" and targets else targets[-1] if targets else target_hint

    if not _level_trade_ready(direction, entry, stop_hint, primary_target):
        return None

    stop = float(stop_hint)  # type: ignore[arg-type]
    target = float(primary_target)  # type: ignore[arg-type]
    risk = abs(entry - stop)
    exit_idx = end_idx
    exit_price = candles[end_idx].close
    exit_reason = "timeout"
    targets_hit = 0
    same_bar_ambiguous = False
    alternate_exit_reason: str | None = None

    for i in range(entry_idx, end_idx + 1):
        candle = candles[i]
        if direction == "bullish":
            stop_hit = candle.low <= stop
            targets_hit_now = sum(1 for level in targets if candle.high >= level)
        else:
            stop_hit = candle.high >= stop
            targets_hit_now = sum(1 for level in targets if candle.low <= level)
        target_hit = targets_hit_now > 0
        if stop_hit and target_hit:
            same_bar_ambiguous = True
            alternate_exit_reason = "target_same_bar" if same_bar_policy != "target_first" else "stop_same_bar"
            if same_bar_policy == "target_first":
                targets_hit = max(targets_hit, targets_hit_now)
                target_exit = targets[min(targets_hit_now, len(targets)) - 1]
                exit_idx, exit_price, exit_reason = i, target_exit, "target_same_bar"
            else:
                exit_idx, exit_price, exit_reason = i, stop, "stop_same_bar"
            break
        if stop_hit:
            exit_idx, exit_price, exit_reason = i, stop, "stop"
            break
        if target_hit:
            targets_hit = max(targets_hit, targets_hit_now)
            if targets_hit >= len(targets):
                exit_idx, exit_price, exit_reason = i, targets[-1], "target"
                break

    signed = ((exit_price / entry) - 1.0) if direction == "bullish" else ((entry / exit_price) - 1.0)
    target_progress = (targets_hit / len(targets)) if targets else 0.0
    label = "flat"
    if exit_reason.startswith("target") and target_progress >= 1.0:
        label = "win"
    elif target_progress > 0:
        label = "partial_win"
    elif exit_reason.startswith("stop"):
        label = "loss"
    elif signed >= win_thresh_pct:
        label = "win"
    elif signed <= loss_thresh_pct:
        label = "loss"

    path = candles[entry_idx : end_idx + 1]
    if direction == "bullish":
        mfe = max((c.high / entry) - 1.0 for c in path)
        mae = min((c.low / entry) - 1.0 for c in path)
        r_mult = (exit_price - entry) / risk if risk else 0.0
    else:
        mfe = max((entry / c.low) - 1.0 for c in path if c.low > 0)
        mae = min((entry / c.high) - 1.0 for c in path if c.high > 0)
        r_mult = (entry - exit_price) / risk if risk else 0.0

    return {
        "evaluation_method": "target_stop",
        "entry_source": entry_source,
        "entry_price": round(entry, 4),
        "stop_price": round(stop, 4),
        "target_price": round(target, 4),
        "target_prices": [round(x, 4) for x in targets],
        "targets_hit_count": targets_hit,
        "target_count": len(targets),
        "target_progress": round(target_progress, 4),
        "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "same_bar_policy": same_bar_policy,
        "same_bar_ambiguous": same_bar_ambiguous,
        "same_bar_alternate_exit_reason": alternate_exit_reason,
        "exit_bars": int(exit_idx - entry_idx),
        "net_return_pct": round(signed * 100, 3),
        # Keep this field for existing ranking/report compatibility. For level-based
        # calls it is the actual simulated trade return, not benchmark-adjusted edge.
        "benchmark_excess_return_pct": round(signed * 100, 3),
        "r_multiple": round(r_mult, 4),
        "max_favorable_excursion": round(mfe * 100, 3),
        "max_adverse_excursion": round(mae * 100, 3),
        "label": label,
        "outcome_credit": round(1.0 if label == "win" else target_progress if label == "partial_win" else 0.0, 4),
    }


def outcome_for_call(
    symbol: str,
    direction: str,
    sent_at: datetime,
    horizons: List[int],
    symbol_candles: List[Candle],
    benchmark_candles: List[Candle] | None,
    win_thresh_pct: float,
    loss_thresh_pct: float,
    intent: str = "",
    entry_hint: float | None = None,
    stop_hint: float | None = None,
    target_hint: float | None = None,
    target_hints: List[float] | None = None,
    trigger_above: float | None = None,
    trigger_below: float | None = None,
    prefer_target_stop: bool = True,
    same_bar_policy: str = "stop_first",
) -> List[Dict[str, Any]]:
    idx = closest_idx_at_or_after(symbol_candles, sent_at)
    if idx is None:
        return []

    bench_idx = closest_idx_at_or_after(benchmark_candles, sent_at) if benchmark_candles else None
    out: List[Dict[str, Any]] = []
    for h in horizons:
        if idx + h >= len(symbol_candles):
            continue

        level_result = None
        if prefer_target_stop:
            level_result = _simulate_target_stop_outcome(
                direction=direction,
                intent=intent,
                sent_idx=idx,
                horizon=h,
                candles=symbol_candles,
                entry_hint=entry_hint,
                stop_hint=stop_hint,
                target_hint=target_hint,
                target_hints=target_hints,
                trigger_above=trigger_above,
                trigger_below=trigger_below,
                win_thresh_pct=win_thresh_pct,
                loss_thresh_pct=loss_thresh_pct,
                same_bar_policy=same_bar_policy,
            )
        if level_result is not None:
            out.append({"evaluation_window": f"{h}d", **level_result})
            continue

        entry = symbol_candles[idx].close
        exit_ = symbol_candles[idx + h].close
        raw_ret = (exit_ / entry) - 1.0 if entry else 0.0
        signed = raw_ret if direction == "bullish" else -raw_ret

        bench_excess = signed
        if benchmark_candles and bench_idx is not None and bench_idx + h < len(benchmark_candles):
            b0 = benchmark_candles[bench_idx].close
            b1 = benchmark_candles[bench_idx + h].close
            bench_ret = (b1 / b0) - 1.0 if b0 else 0.0
            bench_excess = signed - bench_ret

        if bench_excess >= win_thresh_pct:
            label = "win"
        elif bench_excess <= loss_thresh_pct:
            label = "loss"
        else:
            label = "flat"

        path = symbol_candles[idx : idx + h + 1]
        mfe = 0.0
        mae = 0.0
        if path:
            if direction == "bullish":
                highs = [c.high for c in path]
                lows = [c.low for c in path]
                mfe = max((x / entry) - 1.0 for x in highs)
                mae = min((x / entry) - 1.0 for x in lows)
            else:
                lows = [c.low for c in path]
                highs = [c.high for c in path]
                mfe = max((entry / x) - 1.0 for x in lows if x > 0)
                mae = min((entry / x) - 1.0 for x in highs if x > 0)

        out.append(
            {
                "evaluation_window": f"{h}d",
                "evaluation_method": "directional_horizon",
                "entry_source": "next_close",
                "entry_price": round(entry, 4),
                "exit_price": round(exit_, 4),
                "exit_reason": "horizon",
                "net_return_pct": round(signed * 100, 3),
                "benchmark_excess_return_pct": round(bench_excess * 100, 3),
                "r_multiple": None,
                "max_favorable_excursion": round(mfe * 100, 3),
                "max_adverse_excursion": round(mae * 100, 3),
                "label": label,
                "outcome_credit": 1.0 if label == "win" else 0.0,
            }
        )
    return out


def excluded_outcome(reason: str, label: str = "flat") -> Dict[str, Any]:
    return {
        "evaluation_window": "n/a",
        "evaluation_method": reason,
        "entry_source": None,
        "entry_price": None,
        "exit_price": None,
        "exit_reason": reason,
        "net_return_pct": None,
        "benchmark_excess_return_pct": None,
        "r_multiple": None,
        "max_favorable_excursion": None,
        "max_adverse_excursion": None,
        "label": label,
        "outcome_credit": 0.0,
        "exclude_from_performance": True,
        "exclusion_reason": reason,
    }


def pre_market_exclusion_reason(call: ParsedCall) -> str | None:
    if call.is_continuation and call.parent_call_id:
        return "continuation_update"
    if call.instrument_type == "options":
        return "options_no_premium_data"
    if call.instrument_type == "index" and not call.symbol.startswith("^"):
        return "index_market_symbol_unmapped"
    return None


def bayes_win_rate(wins: int, losses: int, flats: int, alpha: float = 4.0, beta: float = 4.0) -> float:
    denom = wins + losses + flats + alpha + beta
    return (wins + alpha) / denom if denom else 0.5


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _sample_stddev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def _mean_t_stat(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    stddev = _sample_stddev(values)
    if stddev <= 0:
        return 0.0
    mean = sum(values) / len(values)
    return mean / (stddev / math.sqrt(len(values)))


def _wilson_interval(success_credit: float, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p_hat = max(0.0, min(1.0, success_credit / total))
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p_hat + z2 / (2.0 * total)) / denom
    margin = (z / denom) * math.sqrt((p_hat * (1.0 - p_hat)) / total + z2 / (4.0 * total * total))
    return max(0.0, center - margin), min(1.0, center + margin)


def _sample_reliability(call_count: int, resolved_count: int, t_stat: float) -> float:
    count_component = min(1.0, math.sqrt(max(0, call_count) / 30.0))
    resolution_component = min(1.0, _safe_div(resolved_count, max(1, call_count)))
    significance_component = min(1.0, abs(t_stat) / 2.0)
    reliability = 0.5 * count_component + 0.3 * resolution_component + 0.2 * significance_component
    return max(0.0, min(1.0, reliability))


def _evidence_grade(call_count: int, resolved_count: int, t_stat: float, win_ci_low: float, win_ci_high: float) -> str:
    if call_count <= 0:
        return "no_data"
    if call_count < 8 or resolved_count < 5:
        return "insufficient_sample"
    if t_stat <= -1.0 or win_ci_high < 0.5:
        return "negative_edge"
    if call_count >= 30 and resolved_count >= 20 and t_stat >= 2.0 and win_ci_low > 0.5:
        return "strong_edge"
    if call_count >= 15 and resolved_count >= 10 and t_stat >= 1.0 and win_ci_low >= 0.45:
        return "suggestive_edge"
    return "exploratory"


def _row_label(row: Dict[str, Any]) -> str:
    return str(row.get("label") or "flat")


def _row_method(row: Dict[str, Any]) -> str:
    return str(row.get("evaluation_method") or "unknown")


def _row_is_resolved(row: Dict[str, Any]) -> bool:
    return _row_label(row) in {"win", "partial_win", "loss"}


def _row_is_scored(row: Dict[str, Any]) -> bool:
    return not bool(row.get("exclude_from_performance"))


def _row_outcome_credit(row: Dict[str, Any]) -> float:
    value = row.get("outcome_credit")
    if value is not None:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0
    return 1.0 if _row_label(row) == "win" else 0.0


def _row_return(row: Dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _row_horizon_days(row: Dict[str, Any]) -> int:
    window = str(row.get("evaluation_window") or "").strip()
    if window.endswith("d"):
        try:
            return int(window[:-1])
        except ValueError:
            return 0
    return 0


def _select_primary_call_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_call: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        call_id = str(row.get("call_id") or "")
        if call_id:
            by_call[call_id].append(row)

    primary: List[Dict[str, Any]] = []
    for call_rows in by_call.values():
        primary.append(
            sorted(
                call_rows,
                key=lambda row: (
                    0 if _row_is_resolved(row) else 1,
                    0 if _row_method(row) == "target_stop" else 1,
                    _row_horizon_days(row),
                ),
            )[0]
        )
    return primary


def _metric_block(rows: List[Dict[str, Any]], return_key: str) -> Dict[str, Any]:
    excluded = len([row for row in rows if not _row_is_scored(row)])
    rows = [row for row in rows if _row_is_scored(row)]
    total = len(rows)
    wins = sum(1 for row in rows if _row_label(row) == "win")
    partial_wins = sum(1 for row in rows if _row_label(row) == "partial_win")
    credit_wins = sum(_row_outcome_credit(row) for row in rows if _row_label(row) in {"win", "partial_win"})
    losses = sum(1 for row in rows if _row_label(row) == "loss")
    flats = sum(1 for row in rows if _row_label(row) == "flat")
    resolved = wins + partial_wins + losses
    resolved_rows = [row for row in rows if _row_is_resolved(row)]
    resolved_returns = [_row_return(row, return_key) for row in resolved_rows]
    win_returns = [value for value in resolved_returns if value > 0]
    loss_returns = [abs(value) for value in resolved_returns if value < 0]
    avg_win = (sum(win_returns) / len(win_returns)) if win_returns else 0.0
    avg_loss = (sum(loss_returns) / len(loss_returns)) if loss_returns else 0.0
    expectancy = (sum(resolved_returns) / len(resolved_returns)) if resolved_returns else 0.0
    median_return = median(resolved_returns) if resolved_returns else 0.0
    return_stddev = _sample_stddev(resolved_returns)
    return_sem = return_stddev / math.sqrt(len(resolved_returns)) if resolved_returns and return_stddev else 0.0
    t_stat = _mean_t_stat(resolved_returns)
    risk_adjusted_return = _safe_div(expectancy, return_stddev) if return_stddev else 0.0
    win_ci_low, win_ci_high = _wilson_interval(credit_wins, total)
    resolved_win_ci_low, resolved_win_ci_high = _wilson_interval(credit_wins, resolved)
    reliability = _sample_reliability(total, resolved, t_stat)
    evidence_grade = _evidence_grade(total, resolved, t_stat, win_ci_low, win_ci_high)

    return {
        "rows_count": total,
        "excluded_rows_count": excluded,
        "resolved_rows_count": len(resolved_rows),
        "win_count": wins,
        "partial_win_count": partial_wins,
        "outcome_credit_sum": round(credit_wins, 4),
        "loss_count": losses,
        "flat_count": flats,
        "win_rate": round(_safe_div(credit_wins, total), 4),
        "resolved_win_rate": round(_safe_div(credit_wins, resolved), 4),
        "flat_rate": round(_safe_div(flats, total), 4),
        "expectancy": round(expectancy, 4),
        "median_return": round(median_return, 4),
        "avg_return_pct": round(expectancy, 4),
        "median_return_pct": round(median_return, 4),
        "return_stddev": round(return_stddev, 4),
        "return_sem": round(return_sem, 4),
        "excess_return_t_stat": round(t_stat, 4),
        "risk_adjusted_return": round(risk_adjusted_return, 4),
        "win_rate_ci_low": round(win_ci_low, 4),
        "win_rate_ci_high": round(win_ci_high, 4),
        "resolved_win_rate_ci_low": round(resolved_win_ci_low, 4),
        "resolved_win_rate_ci_high": round(resolved_win_ci_high, 4),
        "sample_reliability": round(reliability, 4),
        "evidence_grade": evidence_grade,
        "payoff_ratio": round(_safe_div(avg_win, avg_loss), 4),
        "profit_factor": round(_safe_div(sum(win_returns), sum(loss_returns)), 4),
        "bayes_win_rate": round(bayes_win_rate(credit_wins, losses, flats), 4),
    }


def compute_metrics_v2(
    rows: List[Dict[str, Any]],
    parsed_calls_count: int | None = None,
    message_count: int | None = None,
) -> Dict[str, Any]:
    by_call: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        call_id = str(row.get("call_id") or "")
        if call_id:
            by_call[call_id].append(row)

    scored_rows = [row for row in rows if _row_is_scored(row)]
    primary_rows = _select_primary_call_rows(scored_rows)
    resolved_primary_rows = [row for row in primary_rows if _row_is_resolved(row)]
    resolved_rows = [row for row in scored_rows if _row_is_resolved(row)]
    target_stop_rows = [row for row in rows if _row_method(row) == "target_stop"]
    directional_rows = [row for row in rows if _row_method(row) == "directional_horizon"]

    row_block = _metric_block(rows, "net_return_pct")
    call_block = _metric_block(primary_rows, "net_return_pct")
    target_stop_block = _metric_block(target_stop_rows, "net_return_pct")
    directional_block = _metric_block(directional_rows, "benchmark_excess_return_pct")

    call_count = len(primary_rows)
    row_count = len(scored_rows)
    total_row_count = len(rows)
    resolved_call_count = len(resolved_primary_rows)
    resolved_row_count = len(resolved_rows)
    excluded_rows = [row for row in rows if not _row_is_scored(row)]

    density = {
        "rows_per_call": round(_safe_div(row_count, call_count), 4),
        "resolved_rows_per_call": round(_safe_div(resolved_row_count, call_count), 4),
        "duplicate_rate": round(_safe_div(max(0, row_count - call_count), row_count), 4),
        "calls_per_message": round(_safe_div(call_count, message_count), 4) if message_count else None,
        "rows_per_message": round(_safe_div(row_count, message_count), 4) if message_count else None,
        "actionability_rate": round(_safe_div(call_count, parsed_calls_count), 4) if parsed_calls_count else None,
        "call_coverage_rate": round(_safe_div(call_count, parsed_calls_count), 4) if parsed_calls_count else None,
    }

    return {
        "selection_policy": "resolved rows first, then target_stop before directional_horizon, then shorter horizon",
        "counts": {
            "messages_count": message_count,
            "parsed_calls_count": parsed_calls_count,
            "call_count": call_count,
            "row_count": row_count,
            "total_row_count": total_row_count,
            "resolved_call_count": resolved_call_count,
            "resolved_row_count": resolved_row_count,
            "target_stop_row_count": len(target_stop_rows),
            "directional_horizon_row_count": len(directional_rows),
            "excluded_row_count": len(excluded_rows),
        },
        "density": density,
        "call_level": {
            **call_block,
            "calls_count": call_count,
            "resolved_calls_count": resolved_call_count,
            "actionability_rate": density["actionability_rate"],
            "duplicate_rate": density["duplicate_rate"],
            "rows_per_call": density["rows_per_call"],
            "resolved_rows_per_call": density["resolved_rows_per_call"],
        },
        "row_level": {
            **row_block,
            "rows_count": row_count,
            "duplicate_rate": density["duplicate_rate"],
        },
        "methods": {
            "target_stop": {
                **target_stop_block,
                "target_stop_win_rate": target_stop_block["resolved_win_rate"],
            },
            "directional_horizon": {
                **directional_block,
                "benchmark_relative_win_rate": directional_block["resolved_win_rate"],
            },
        },
        "instrument_types": {
            name: _metric_block([row for row in rows if str(row.get("instrument_type") or "unknown") == name], "benchmark_excess_return_pct")
            for name in sorted({str(row.get("instrument_type") or "unknown") for row in rows})
        },
        "symbols": {
            name: _metric_block([row for row in rows if str(row.get("display_symbol") or row.get("symbol") or "unknown") == name], "benchmark_excess_return_pct")
            for name in sorted({str(row.get("display_symbol") or row.get("symbol") or "unknown") for row in rows})
        },
        "compat": {
            "legacy_row_win_rate": row_block["win_rate"],
            "legacy_call_win_rate": call_block["win_rate"],
        },
    }


def recency_weight(sent_at_iso: str, now: datetime, half_life_days: float) -> float:
    try:
        dt = datetime.fromisoformat(sent_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except ValueError:
        return 1.0
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def score_bucket(records: List[Dict[str, Any]], now: datetime, half_life_days: float) -> Dict[str, Any]:
    call_metrics = compute_metrics_v2(records)
    scored_records = [record for record in records if _row_is_scored(record)]
    if not scored_records:
        return {
            "calls_count": 0,
            "valid_calls_count": 0,
            "rows_count": len(records),
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "payoff_ratio": 0.0,
            "downside_tail": 0.0,
            "calibration_error": 1.0,
            "return_stddev": 0.0,
            "risk_adjusted_return": 0.0,
            "excess_return_t_stat": 0.0,
            "sample_reliability": 0.0,
            "evidence_grade": "no_data",
            "raw_score": 0.0,
            "recency_weighted_score": 0.0,
            "tier": "D",
            "metrics_v2": call_metrics,
        }
    returns = [float(r.get("benchmark_excess_return_pct") or 0.0) for r in scored_records]
    wins = sum(_row_outcome_credit(r) for r in scored_records if r["label"] in {"win", "partial_win"})
    losses = sum(1 for r in scored_records if r["label"] == "loss")
    flats = sum(1 for r in scored_records if r["label"] == "flat")
    win_rate = wins / len(scored_records)
    win_rate_b = bayes_win_rate(wins, losses, flats)
    avg_ret = sum(returns) / len(returns)
    med_ret = median(returns)
    win_vals = [x for x in returns if x > 0]
    loss_vals = [abs(x) for x in returns if x < 0]
    payoff = (sum(win_vals) / len(win_vals)) / (sum(loss_vals) / len(loss_vals)) if win_vals and loss_vals else 0.0
    p10 = sorted(returns)[max(0, int(len(returns) * 0.1) - 1)] if returns else 0.0

    cal_err = 0.0
    for r in scored_records:
        realized = _row_outcome_credit(r)
        cal_err += abs(float(r["parser_confidence"]) - realized)
    cal_err = cal_err / len(scored_records)

    wr_component = win_rate_b * 100.0
    ret_component = max(-5.0, min(5.0, med_ret)) * 4.0
    payoff_component = min(3.0, payoff) * 8.0
    tail_penalty = max(0.0, abs(min(0.0, p10))) * 1.5
    conf_penalty = cal_err * 12.0

    rec_w_sum = 0.0
    rec_n = 0.0
    for r in scored_records:
        w = recency_weight(r["sent_at_utc"], now, half_life_days)
        rec_w_sum += w * float(r.get("benchmark_excess_return_pct") or 0.0)
        rec_n += w
    recency_edge = (rec_w_sum / rec_n) if rec_n else 0.0

    raw = wr_component + ret_component + payoff_component - tail_penalty - conf_penalty + (recency_edge * 1.2)
    call_m = call_metrics["call_level"]
    reliability = float(call_m.get("sample_reliability") or 0.0)
    return_stddev = float(call_m.get("return_stddev") or 0.0)
    volatility_penalty = min(15.0, return_stddev * 0.25)
    score = max(0.0, min(100.0, raw * (0.5 + 0.5 * reliability) - volatility_penalty))
    if score >= 80:
        tier = "A"
    elif score >= 65:
        tier = "B"
    elif score >= 45:
        tier = "C"
    else:
        tier = "D"
    return {
        "calls_count": call_metrics["call_level"]["calls_count"],
        "valid_calls_count": call_metrics["call_level"]["resolved_calls_count"],
        "rows_count": call_metrics["row_level"]["rows_count"],
        "win_rate": round(win_rate, 4),
        "avg_return": round(avg_ret, 4),
        "median_return": round(med_ret, 4),
        "payoff_ratio": round(payoff, 4),
        "downside_tail": round(p10, 4),
        "calibration_error": round(cal_err, 4),
        "return_stddev": round(return_stddev, 4),
        "risk_adjusted_return": call_m.get("risk_adjusted_return", 0.0),
        "excess_return_t_stat": call_m.get("excess_return_t_stat", 0.0),
        "win_rate_ci_low": call_m.get("win_rate_ci_low", 0.0),
        "win_rate_ci_high": call_m.get("win_rate_ci_high", 0.0),
        "resolved_win_rate_ci_low": call_m.get("resolved_win_rate_ci_low", 0.0),
        "resolved_win_rate_ci_high": call_m.get("resolved_win_rate_ci_high", 0.0),
        "sample_reliability": round(reliability, 4),
        "evidence_grade": call_m.get("evidence_grade", "no_data"),
        "raw_score": round(max(0.0, min(100.0, raw)), 3),
        "recency_weighted_score": round(score, 3),
        "tier": tier,
        "metrics_v2": call_metrics,
    }


async def fetch_messages(
    api_id: int,
    api_hash: str,
    session_path: str,
    sources: List[Dict[str, Any]],
    since: datetime,
    max_messages_per_channel: int,
) -> List[Dict[str, Any]]:
    from telethon import TelegramClient  # type: ignore

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    messages: List[Dict[str, Any]] = []
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session not authorized.")
        for src in sources:
            handle = str(src.get("handle") or "").strip()
            if not handle:
                continue
            try:
                entity = await client.get_entity(handle)
            except Exception as error:
                messages.append(
                    {
                        "channel_id": None,
                        "channel_handle": handle,
                        "author_id": None,
                        "author_name": "",
                        "message_id": None,
                        "sent_at_utc": None,
                        "text_raw": "",
                        "is_forwarded": False,
                        "reply_to_id": None,
                        "message_meta": {"error": str(error)},
                    }
                )
                continue

            async for msg in client.iter_messages(entity, limit=max_messages_per_channel):
                if msg.date is None:
                    continue
                msg_dt = msg.date.astimezone(UTC)
                if msg_dt < since:
                    break
                text = getattr(msg, "message", None) or ""
                if not text.strip():
                    continue
                sender = getattr(msg, "sender", None)
                author_id = getattr(sender, "id", None) or getattr(msg, "sender_id", None)
                author_name = (
                    getattr(sender, "username", None)
                    or " ".join(
                        x for x in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if x
                    ).strip()
                    or ""
                )
                messages.append(
                    {
                        "channel_id": int(getattr(entity, "id", 0) or 0),
                        "channel_handle": handle,
                        "author_id": int(author_id) if author_id is not None else None,
                        "author_name": author_name,
                        "message_id": int(getattr(msg, "id", 0) or 0),
                        "sent_at_utc": msg_dt.isoformat(),
                        "text_raw": normalize_text(text),
                        "is_forwarded": bool(getattr(msg, "fwd_from", None)),
                        "reply_to_id": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
                        "message_meta": {
                            "views": getattr(msg, "views", None),
                            "forwards": getattr(msg, "forwards", None),
                        },
                    }
                )
    finally:
        await client.disconnect()
    return messages


def _message_sort_key(item: tuple[int, Dict[str, Any]]) -> tuple[str, int]:
    idx, message = item
    return str(message.get("sent_at_utc") or ""), idx


def _message_source_key(message: Dict[str, Any]) -> str:
    if message.get("author_id") is not None:
        return f"author:{message.get('author_id')}"
    if message.get("channel_id") is not None:
        return f"channel:{message.get('channel_id')}"
    return f"channel:{message.get('channel_handle') or 'unknown'}"


def _continuation_base_key(source_key: str, instrument_type: str, symbol: str | None) -> str | None:
    if not symbol:
        return None
    return f"{source_key}:{instrument_type}:{symbol}"


def parse_calls(
    messages: Iterable[Dict[str, Any]],
    symbol_map: Dict[str, str],
    llm_verify_enabled: bool = False,
    llm_verify_endpoint: str = "http://127.0.0.1:11434",
    llm_verify_model: str = "phi4-mini",
    llm_verify_timeout_sec: int = 15,
    llm_verify_mode: str = "review_only",
    llm_extract_enabled: bool = False,
    llm_extract_endpoint: str = "https://api.openai.com/v1",
    llm_extract_model: str = "gpt-4.1-mini",
    llm_extract_api_key: str = "",
    llm_extract_timeout_sec: int = 30,
) -> List[ParsedCall]:
    out: List[ParsedCall] = []
    active_parent_by_key: Dict[str, tuple[str, str]] = {}
    for _, m in sorted(enumerate(messages), key=_message_sort_key):
        text = str(m.get("text_raw") or "")
        if not text:
            continue
        intent, direction, conf = classify_intent(text)
        source_key = _message_source_key(m)
        syms = symbol_candidates(text, symbol_map)
        provisional_direction = direction if direction in {"bullish", "bearish"} else "bullish"
        instrument = detect_instrument(text, syms, symbol_map, provisional_direction)
        if instrument.symbol and not syms and instrument.instrument_type in {"index", "options"}:
            syms = [instrument.symbol]
        if is_non_actionable_news_context(text, syms):
            continue
        is_update_only = intent in {"noise", "ambiguous", "wait"} and instrument.is_continuation
        if intent in {"noise", "ambiguous", "wait"} and not is_update_only:
            continue
        entry_hint = parse_num(ENTRY_PAT, text)
        stop_hint = parse_num(STOP_PAT, text)
        target_hint = parse_num(TGT_PAT, text)
        trigger_above = parse_num(ABOVE_PAT, text)
        trigger_below = parse_num(BELOW_PAT, text)
        verifier_verdict, verifier_reason = ("accept", "continuation_update") if is_update_only else verify_message_intent(text, intent, direction)
        if llm_verify_enabled:
            should_call_llm = (
                not is_update_only
                and (
                llm_verify_mode == "always"
                or (llm_verify_mode == "review_only" and verifier_verdict == "review")
                )
            )
            if should_call_llm:
                llm_v, llm_r = llm_verify_message_ollama(
                    text=text,
                    intent=intent,
                    direction=direction,
                    endpoint=llm_verify_endpoint,
                    model=llm_verify_model,
                    timeout_sec=llm_verify_timeout_sec,
                )
                if verifier_verdict == "accept" and llm_v == "reject":
                    verifier_verdict = "reject"
                    verifier_reason = f"llm:{llm_r}"
                elif verifier_verdict == "review":
                    verifier_verdict = llm_v
                    verifier_reason = f"llm:{llm_r}"
        if verifier_verdict == "reject":
            continue
        extracted_target_hints: List[float] = []
        if llm_extract_enabled and llm_extract_api_key and _should_attempt_llm_extract(
            verifier_verdict,
            conf,
            syms,
            entry_hint,
            stop_hint,
            target_hint,
            raw_text=text,
        ):
            try:
                extracted = llm_extract_call_openai_compatible(
                    text=text,
                    endpoint=llm_extract_endpoint,
                    model=llm_extract_model,
                    api_key=llm_extract_api_key,
                    timeout_sec=llm_extract_timeout_sec,
                )
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                extracted = {}

            extracted_symbol = _normalize_symbol(extracted.get("symbol"), symbol_map)
            extracted_direction = str(extracted.get("direction") or "").strip().lower()
            if extracted_direction in {"bullish", "bearish"} and extracted_direction == direction:
                if not syms and extracted_symbol:
                    syms = [extracted_symbol]
                if entry_hint is None:
                    entry_hint = _safe_float(extracted.get("entry"))
                if trigger_above is None:
                    trigger_above = _safe_float(extracted.get("trigger_above"))
                if trigger_below is None:
                    trigger_below = _safe_float(extracted.get("trigger_below"))
                llm_entry = entry_hint if entry_hint is not None else _safe_float(extracted.get("entry"))
                llm_stop = stop_hint if stop_hint is not None else _safe_float(extracted.get("stop_loss"))
                llm_target = target_hint if target_hint is not None else _safe_float(extracted.get("target"))
                valid_entry, valid_stop, valid_target = _validated_llm_levels(
                    direction,
                    llm_entry,
                    llm_stop,
                    llm_target,
                )
                if stop_hint is None and valid_stop is not None:
                    stop_hint = valid_stop
                if target_hint is None and valid_target is not None:
                    target_hint = valid_target
                extracted_targets = extracted.get("targets")
                if isinstance(extracted_targets, list):
                    for value in extracted_targets:
                        parsed_target = _safe_float(value)
                        if parsed_target and parsed_target > 0 and parsed_target not in extracted_target_hints:
                            extracted_target_hints.append(parsed_target)
                if entry_hint is None and valid_entry is not None:
                    entry_hint = valid_entry
                if extracted and (valid_stop is not None or valid_target is not None or extracted_symbol):
                    verifier_reason = f"{verifier_reason}|llm_extract"
        instrument = detect_instrument(text, syms, symbol_map, provisional_direction)
        for parsed_target in extracted_target_hints:
            if parsed_target not in instrument.target_hints:
                instrument.target_hints.append(parsed_target)
        if entry_hint is None and instrument.entry_hint is not None:
            entry_hint = instrument.entry_hint
        if target_hint is None and instrument.target_hints:
            target_hint = instrument.target_hints[0]
        if not syms:
            continue
        if direction not in {"bullish", "bearish"} and not is_update_only:
            continue
        for idx, sym in enumerate(syms[:3]):
            per_symbol_instrument = instrument
            if instrument.instrument_type not in {"options", "index"}:
                per_symbol_instrument = detect_instrument(text, [sym], symbol_map, provisional_direction)
                for parsed_target in extracted_target_hints:
                    if parsed_target not in per_symbol_instrument.target_hints:
                        per_symbol_instrument.target_hints.append(parsed_target)
            parent_call_id = None
            effective_direction = direction
            base_symbol = per_symbol_instrument.underlying or per_symbol_instrument.display_symbol or per_symbol_instrument.symbol
            parent_base_key = _continuation_base_key(source_key, per_symbol_instrument.instrument_type, base_symbol)
            if per_symbol_instrument.is_continuation and per_symbol_instrument.parent_key:
                parent_record = active_parent_by_key.get(parent_base_key or "")
                if parent_record:
                    parent_call_id, effective_direction = parent_record
            if is_update_only and not parent_call_id:
                continue
            call_id = f"{m.get('channel_id')}:{m.get('message_id')}:{sym}:{idx}"
            if parent_call_id is None and parent_base_key and effective_direction in {"bullish", "bearish"}:
                active_parent_by_key[parent_base_key] = (call_id, effective_direction)
            out.append(
                ParsedCall(
                    call_id=call_id,
                    message_id=int(m.get("message_id") or 0),
                    channel_id=int(m.get("channel_id") or 0),
                    channel_handle=str(m.get("channel_handle") or ""),
                    author_id=(int(m["author_id"]) if m.get("author_id") is not None else None),
                    author_name=str(m.get("author_name") or ""),
                    sent_at_utc=str(m.get("sent_at_utc") or ""),
                    symbol=per_symbol_instrument.symbol or sym,
                    display_symbol=per_symbol_instrument.display_symbol or sym,
                    instrument_type=per_symbol_instrument.instrument_type,
                    underlying=per_symbol_instrument.underlying,
                    options_details=instrument_options_details(per_symbol_instrument),
                    direction=effective_direction,
                    call_type="continuation" if parent_call_id else call_type(text),
                    intent="continuation_update" if is_update_only else intent,
                    parser_confidence=round(conf, 3),
                    entry_hint=entry_hint,
                    stop_hint=stop_hint,
                    target_hint=target_hint,
                    target_hints=per_symbol_instrument.target_hints or ([target_hint] if target_hint else []),
                    trigger_above=trigger_above,
                    trigger_below=trigger_below,
                    is_continuation=bool(parent_call_id),
                    parent_call_id=parent_call_id,
                    trailing_stop_rule=per_symbol_instrument.trailing_stop_rule,
                    text=text[:400],
                    verifier_verdict=verifier_verdict,
                    verifier_reason=verifier_reason,
                )
            )
    return out


def time_bucket(iso_ts: str, freq: str) -> str:
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    d = dt.date()
    if freq == "month":
        return f"{d.year:04d}-{d.month:02d}"
    if freq == "week":
        y, w, _ = d.isocalendar()
        return f"{y:04d}-W{w:02d}"
    return d.isoformat()


def build_time_series(
    outcomes: List[Dict[str, Any]],
    symbol_sector_map: Dict[str, str],
    freq: str,
) -> Dict[str, Any]:
    by_author_period: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_author_symbol_period: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_author_sector_period: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        author_key = str(row["author_id"]) if row.get("author_id") is not None else f"name:{row.get('author_name') or 'unknown'}"
        period = time_bucket(str(row["sent_at_utc"]), freq)
        symbol = str(row["symbol"])
        sector = symbol_sector_map.get(symbol, "unknown")
        by_author_period[f"{author_key}::{period}"].append(row)
        by_author_symbol_period[f"{author_key}::{symbol}::{period}"].append(row)
        by_author_sector_period[f"{author_key}::{sector}::{period}"].append(row)

    def summarize(items: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        out = []
        for k, rows in items.items():
            scored_rows = [row for row in rows if _row_is_scored(row)]
            primary_rows = _select_primary_call_rows(scored_rows)
            wins = sum(_row_outcome_credit(r) for r in scored_rows if r["label"] in {"win", "partial_win"})
            losses = sum(1 for r in scored_rows if r["label"] == "loss")
            flats = sum(1 for r in scored_rows if r["label"] == "flat")
            resolved_rows = [r for r in scored_rows if _row_is_resolved(r)]
            rets = [float(r.get("benchmark_excess_return_pct") or 0.0) for r in scored_rows]
            avg = sum(rets) / len(rets) if rets else 0.0
            out.append(
                {
                    "key": k,
                    "calls_count": len(primary_rows),
                    "rows_count": len(rows),
                    "excluded_rows_count": len(rows) - len(scored_rows),
                    "valid_calls_count": sum(1 for r in primary_rows if _row_is_resolved(r)),
                    "resolved_rows_count": len(resolved_rows),
                    "wins": wins,
                    "losses": losses,
                    "flats": flats,
                    "win_rate": round(wins / len(rows), 4) if rows else 0.0,
                    "resolved_win_rate": round(_safe_div(wins, wins + losses), 4),
                    "avg_excess_return_pct": round(avg, 4),
                }
            )
        out.sort(key=lambda x: (x["key"], x["calls_count"]))
        return out

    return {
        "frequency": freq,
        "author_period": summarize(by_author_period),
        "author_symbol_period": summarize(by_author_symbol_period),
        "author_sector_period": summarize(by_author_sector_period),
    }


def build_rankings(
    outcomes: List[Dict[str, Any]],
    now: datetime,
    half_life_days: float,
    symbol_sector_map: Dict[str, str],
) -> Dict[str, Any]:
    by_channel: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    channel_authors: Dict[str, set[str]] = defaultdict(set)
    by_author: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_pair: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_author_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_author_sector: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        by_channel[str(row["channel_handle"])].append(row)
        key_author = str(row["author_id"]) if row.get("author_id") is not None else f"name:{row.get('author_name') or 'unknown'}"
        channel_authors[str(row["channel_handle"])].add(key_author)
        by_author[key_author].append(row)
        by_pair[f"{row['channel_handle']}::{key_author}"].append(row)
        symbol = str(row["symbol"])
        by_symbol[symbol].append(row)
        by_author_symbol[f"{key_author}::{symbol}"].append(row)
        sector = symbol_sector_map.get(symbol, "unknown")
        by_author_sector[f"{key_author}::{sector}"].append(row)

    def score_map(items: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        ranked: List[Dict[str, Any]] = []
        for k, rows in items.items():
            stats = score_bucket(rows, now, half_life_days)
            ranked.append({"key": k, **stats})
        ranked.sort(key=lambda x: (x["recency_weighted_score"], x["calls_count"]), reverse=True)
        return ranked

    channel_rankings_all = score_map(by_channel)
    channel_rankings_single_author = [
        row for row in channel_rankings_all if len(channel_authors.get(str(row["key"]), set())) <= 1
    ]

    return {
        "channel_rankings": channel_rankings_single_author,
        "channel_rankings_all": channel_rankings_all,
        "author_rankings": score_map(by_author),
        "channel_author_rankings": score_map(by_pair),
        "symbol_rankings": score_map(by_symbol),
        "author_symbol_rankings": score_map(by_author_symbol),
        "author_sector_rankings": score_map(by_author_sector),
    }


_PUBLIC_CHANNEL_LABELS: Dict[str, str] = {
    "motilaloswalofficial": "Motilal Oswal Official",
    "nirmalbangofficial": "Nirmal Bang Official",
    "Official_AngelOne": "Angel One",
}

_MASK_ADJECTIVES = [
    "Alpha", "Beta", "Delta", "Sigma", "Swift", "Bold", "Iron", "Silver",
    "Gold", "Dark", "Bright", "Keen", "Rapid", "Sharp", "Steel", "Phantom",
]
_MASK_NOUNS = [
    "Bull", "Eagle", "Hawk", "Wolf", "Tiger", "Fox", "Lion", "Falcon",
    "Shark", "Cobra", "Viper", "Bear", "Panther", "Lynx", "Raven", "Drake",
]


def _codename(seed: str = "") -> str:
    import hashlib
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) if seed else 0
    adj = _MASK_ADJECTIVES[h % len(_MASK_ADJECTIVES)]
    noun = _MASK_NOUNS[(h // len(_MASK_ADJECTIVES)) % len(_MASK_NOUNS)]
    return f"{adj} {noun}"


def _channel_label(handle: str) -> str:
    return _PUBLIC_CHANNEL_LABELS.get(handle, _codename(handle))


def _public_row_key(seed: str) -> str:
    import hashlib

    return f"row-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"


def _public_message_excerpt(text: Any, limit: int = 220) -> str:
    cleaned = normalize_text(str(text or ""))
    cleaned = re.sub(r"https?://\S+|t\.me/\S+", "[link]", cleaned, flags=re.I)
    cleaned = re.sub(r"@\w+", "[handle]", cleaned)
    cleaned = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "[email]", cleaned)
    cleaned = re.sub(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b", "[phone]", cleaned)
    return cleaned[:limit]


def _write_leaderboard(
    payload: Dict[str, Any],
    outcomes: List[Dict[str, Any]],
    id_to_handle: Dict[str, str],
    out_path: Path,
    now: datetime,
    is_threshold: int = 8,
) -> None:
    from collections import defaultdict as _dd
    rankings = payload.get("rankings", {})
    author_rows = rankings.get("author_rankings", [])
    ts_counts: Dict[str, Dict[str, int]] = _dd(lambda: {"target_hits": 0, "stop_hits": 0, "target_stop_rows": 0, "resolved_target_stop_rows": 0})
    author_outcomes: Dict[str, List[Dict[str, Any]]] = _dd(list)
    same_bar_ambiguous_count = 0
    for item in outcomes:
        author_key = str(item.get("author_id")) if item.get("author_id") is not None else f"name:{item.get('author_name') or 'unknown'}"
        author_outcomes[author_key].append(item)
        if item.get("same_bar_ambiguous"):
            same_bar_ambiguous_count += 1
        if str(item.get("evaluation_method") or "") != "target_stop":
            continue
        keys = {str(item.get("author_id") or "").strip(), str(item.get("author_name") or "").strip()}
        keys = {k for k in keys if k}
        reason = str(item.get("exit_reason") or "").strip().lower()
        for key in keys:
            ts_counts[key]["target_stop_rows"] += 1
            if reason.startswith("target"):
                ts_counts[key]["target_hits"] += 1
                ts_counts[key]["resolved_target_stop_rows"] += 1
            elif reason.startswith("stop"):
                ts_counts[key]["stop_hits"] += 1
                ts_counts[key]["resolved_target_stop_rows"] += 1

    rows = []
    drilldowns: Dict[str, Dict[str, Any]] = {}
    rank = 1
    for row in author_rows:
        metrics = row.get("metrics_v2") or {}
        call_m = metrics.get("call_level") or {}
        row_m = metrics.get("row_level") or {}
        method_m = metrics.get("methods") or {}
        dir_m = method_m.get("directional_horizon") or {}
        ts_m = method_m.get("target_stop") or {}
        calls = int(row.get("calls_count") or 0)
        rows_count = int(row.get("rows_count") or row_m.get("rows_count") or 0)
        resolved_calls = int((call_m.get("resolved_calls_count") or call_m.get("resolved_rows_count") or 0))
        raw_key = str(row.get("key") or "")
        row_key = _public_row_key(raw_key)
        handle = id_to_handle.get(raw_key, raw_key)
        ts = ts_counts.get(raw_key) or ts_counts.get(handle) or {"target_hits": 0, "stop_hits": 0, "target_stop_rows": 0, "resolved_target_stop_rows": 0}
        resolved_ts = int(ts["resolved_target_stop_rows"])
        target_hits = int(ts["target_hits"])
        stop_hits = int(ts["stop_hits"])
        tier = "IS" if calls < is_threshold else str(row.get("tier") or "D")
        public_rank = None if tier == "IS" else rank
        if public_rank is not None:
            rank += 1
        instrument_types = metrics.get("instrument_types") or {}
        symbol_metrics = metrics.get("symbols") or {}
        per_symbol = [
            {
                "symbol": symbol,
                "rows_count": block.get("rows_count"),
                "resolved_win_rate": block.get("resolved_win_rate"),
                "expectancy": block.get("expectancy"),
                "avg_return_pct": block.get("avg_return_pct", block.get("expectancy")),
                "return_stddev": block.get("return_stddev"),
                "risk_adjusted_return": block.get("risk_adjusted_return"),
                "excess_return_t_stat": block.get("excess_return_t_stat"),
                "sample_reliability": block.get("sample_reliability"),
                "evidence_grade": block.get("evidence_grade"),
                "profit_factor": block.get("profit_factor"),
                "excluded_rows_count": block.get("excluded_rows_count"),
            }
            for symbol, block in sorted(
                symbol_metrics.items(),
                key=lambda kv: int((kv[1] or {}).get("rows_count") or 0),
                reverse=True,
            )[:12]
        ]
        row_payload = {
            "row_key": row_key,
            "rank": public_rank,
            "display_name": _codename(raw_key),
            "channel": _channel_label(handle),
            "identity_masked": True,
            "tier": tier,
            "score": None if tier == "IS" else row.get("recency_weighted_score"),
            "calls_evaluated": calls,
            "rows_evaluated": rows_count,
            "resolved_calls": resolved_calls,
            "call_win_rate": call_m.get("win_rate"),
            "resolved_win_rate": call_m.get("resolved_win_rate"),
            "row_win_rate": row_m.get("win_rate"),
            "row_resolved_win_rate": row_m.get("resolved_win_rate"),
            "benchmark_relative_win_rate": dir_m.get("benchmark_relative_win_rate"),
            "target_stop_win_rate": ts_m.get("target_stop_win_rate"),
            "target_hits": target_hits,
            "stop_hits": stop_hits,
            "target_stop_rows": int(ts["target_stop_rows"]),
            "resolved_target_stop_rows": resolved_ts,
            "target_hit_rate": (target_hits / resolved_ts) if resolved_ts else None,
            "stop_hit_rate": (stop_hits / resolved_ts) if resolved_ts else None,
            "bayes_win_rate": call_m.get("bayes_win_rate"),
            "avg_r": call_m.get("expectancy"),
            "median_r": call_m.get("median_return"),
            "avg_return_pct": call_m.get("avg_return_pct", call_m.get("expectancy")),
            "median_return_pct": call_m.get("median_return_pct", call_m.get("median_return")),
            "return_stddev": call_m.get("return_stddev"),
            "risk_adjusted_return": call_m.get("risk_adjusted_return"),
            "excess_return_t_stat": call_m.get("excess_return_t_stat"),
            "win_rate_ci_low": call_m.get("win_rate_ci_low"),
            "win_rate_ci_high": call_m.get("win_rate_ci_high"),
            "resolved_win_rate_ci_low": call_m.get("resolved_win_rate_ci_low"),
            "resolved_win_rate_ci_high": call_m.get("resolved_win_rate_ci_high"),
            "sample_reliability": call_m.get("sample_reliability"),
            "evidence_grade": row.get("evidence_grade") or call_m.get("evidence_grade"),
            "raw_score": row.get("raw_score"),
            "profit_factor": call_m.get("profit_factor"),
            "confidence": "insufficient_sample" if tier == "IS" else str(row.get("evidence_grade") or call_m.get("evidence_grade") or "eligible"),
            "instrument_breakdown": instrument_types,
            "per_symbol_breakdown": per_symbol,
            "excluded_rows_count": (metrics.get("counts") or {}).get("excluded_row_count"),
            "options_no_premium_count": sum(
                1 for item in author_outcomes.get(raw_key, []) if item.get("evaluation_method") == "options_no_premium_data"
            ),
            "continuation_update_count": sum(
                1 for item in author_outcomes.get(raw_key, []) if item.get("evaluation_method") == "continuation_update"
            ),
        }
        rows.append(row_payload)
        drilldowns[row_key] = {
            "row_key": row_key,
            "display_name": row_payload["display_name"],
            "channel": row_payload["channel"],
            "tier": tier,
            "confidence": row_payload["confidence"],
            "calls_evaluated": calls,
            "resolved_win_rate": row_payload["resolved_win_rate"],
            "target_stop_win_rate": row_payload["target_stop_win_rate"],
            "sample_reliability": row_payload["sample_reliability"],
            "evidence_grade": row_payload["evidence_grade"],
            "excess_return_t_stat": row_payload["excess_return_t_stat"],
            "risk_adjusted_return": row_payload["risk_adjusted_return"],
            "return_stddev": row_payload["return_stddev"],
            "instrument_breakdown": instrument_types,
            "per_symbol_breakdown": per_symbol,
            "parsed_calls": [],
        }

        seen_calls: set[str] = set()
        for item in sorted(author_outcomes.get(raw_key, []), key=lambda r: str(r.get("sent_at_utc") or ""), reverse=True):
            call_id = str(item.get("call_id") or "")
            if not call_id or call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            drilldowns[row_key]["parsed_calls"].append(
                {
                    "call_id": call_id,
                    "message_id": item.get("message_id"),
                    "message_ts_utc": item.get("sent_at_utc"),
                    "symbol": item.get("display_symbol") or item.get("symbol"),
                    "instrument_type": item.get("instrument_type"),
                    "underlying": item.get("underlying"),
                    "options_details": item.get("options_details"),
                    "direction": item.get("direction"),
                    "outcome": item.get("label"),
                    "outcome_credit": item.get("outcome_credit"),
                    "evaluation_method": item.get("evaluation_method"),
                    "evaluation_window": item.get("evaluation_window"),
                    "net_return_pct": item.get("net_return_pct"),
                    "benchmark_excess_return_pct": item.get("benchmark_excess_return_pct"),
                    "exclude_from_performance": item.get("exclude_from_performance"),
                    "exclusion_reason": item.get("exclusion_reason"),
                    "message_excerpt": _public_message_excerpt(item.get("text")),
                    "parsed_fields": {
                        "symbol": item.get("symbol"),
                        "display_symbol": item.get("display_symbol"),
                        "instrument_type": item.get("instrument_type"),
                        "direction": item.get("direction"),
                        "entry_hint": item.get("entry_hint"),
                        "stop_hint": item.get("stop_hint"),
                        "target_hint": item.get("target_hint"),
                        "target_hints": item.get("target_hints"),
                        "target_prices": item.get("target_prices"),
                        "targets_hit_count": item.get("targets_hit_count"),
                        "target_count": item.get("target_count"),
                        "trigger_above": item.get("trigger_above"),
                        "trigger_below": item.get("trigger_below"),
                        "is_continuation": item.get("is_continuation"),
                        "parent_call_id": item.get("parent_call_id"),
                        "trailing_stop_rule": item.get("trailing_stop_rule"),
                        "same_bar_policy": item.get("same_bar_policy"),
                        "same_bar_ambiguous": item.get("same_bar_ambiguous"),
                        "same_bar_alternate_exit_reason": item.get("same_bar_alternate_exit_reason"),
                    },
                }
            )
            if len(drilldowns[row_key]["parsed_calls"]) >= 25:
                break

    samples: List[Dict[str, Any]] = []
    seen: set = set()
    for item in sorted(outcomes, key=lambda r: float(r.get("net_return_pct") or 0.0), reverse=True):
        call_id = str(item.get("call_id") or "").strip()
        if not call_id or call_id in seen:
            continue
        seen.add(call_id)
        reason = str(item.get("exit_reason") or "").strip().lower()
        handle = id_to_handle.get(str(item.get("channel_id") or item.get("author_id") or ""), str(item.get("channel_handle") or ""))
        author_seed = str(item.get("author_id") or item.get("author_name") or "")
        samples.append({
            "author_alias": _codename(author_seed),
            "channel_alias": _channel_label(handle),
            "symbol": item.get("display_symbol") or item.get("symbol"),
            "instrument_type": item.get("instrument_type"),
            "direction": item.get("direction"),
            "evaluation_window": item.get("evaluation_window"),
            "evaluation_method": item.get("evaluation_method"),
            "outcome": item.get("label"),
            "outcome_credit": item.get("outcome_credit"),
            "reached_target": reason.startswith("target"),
            "reached_stop": reason.startswith("stop"),
            "net_return_pct": item.get("net_return_pct"),
            "benchmark_excess_return_pct": item.get("benchmark_excess_return_pct"),
            "message_excerpt": _public_message_excerpt(item.get("text")),
            "target_prices": item.get("target_prices"),
            "targets_hit_count": item.get("targets_hit_count"),
            "target_count": item.get("target_count"),
            "exclude_from_performance": item.get("exclude_from_performance"),
            "exclusion_reason": item.get("exclusion_reason"),
        })
        if len(samples) >= 8:
            break

    leaderboard = {
        "generated_at_utc": now.isoformat(),
        "source_generated_at_utc": payload.get("generated_at_utc"),
        "methodology_version": "0.4.0",
        "identity_policy": "masked_random",
        "source_summary": {
            "message_count": payload.get("message_count"),
            "parsed_calls_count": payload.get("parsed_calls_count"),
            "outcome_rows_count": payload.get("outcome_rows_count"),
            "benchmark_symbol": payload.get("benchmark_symbol"),
            "market_data_range": payload.get("market_data_range"),
            "lookback_days": payload.get("lookback_days"),
            "horizons_days": payload.get("horizons_days"),
            "prefer_target_stop": payload.get("prefer_target_stop"),
            "same_bar_policy": payload.get("same_bar_policy"),
            "same_bar_ambiguous_count": same_bar_ambiguous_count,
            "is_threshold": is_threshold,
            "sample_size_policy": f"Rows with fewer than {is_threshold} scored calls are labelled IS.",
            "significance_policy": "Evidence grade uses sample reliability, Wilson win-rate intervals, and benchmark-excess return t-statistics.",
        },
        "metrics_v2": payload.get("metrics_v2"),
        "breakdown_samples": samples,
        "drilldowns": drilldowns,
        "rows": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(leaderboard, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram quality backfill with author/channel performance scoring.")
    ap.add_argument("--config", default="channels.json")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--lookback-days", type=int, default=240)
    ap.add_argument("--max-messages-per-channel", type=int, default=600)
    ap.add_argument("--horizons", default="1,3,5,10")
    ap.add_argument("--benchmark-symbol", default="NIFTYBEES.NS")
    ap.add_argument("--market-data-range", default="1y", help="Candle range for Telegram outcome evaluation, e.g. 1y/2y/5y")
    ap.add_argument("--prefer-target-stop", action=argparse.BooleanOptionalAction, default=True, help="Use target/SL simulation when a call has coherent entry, stop, and target levels")
    ap.add_argument("--same-bar-policy", default="stop_first", choices=["stop_first", "target_first"], help="When target and SL are both inside one daily candle, choose conservative stop_first by default")
    ap.add_argument("--win-threshold-pct", type=float, default=1.0)
    ap.add_argument("--loss-threshold-pct", type=float, default=-1.0)
    ap.add_argument("--half-life-days", type=float, default=30.0)
    ap.add_argument("--out-dir", default="data/output")
    ap.add_argument("--runtime-json", default="data/output/scores.json")
    ap.add_argument("--leaderboard-out", default="public/leaderboard-public.json", help="Write masked public leaderboard JSON directly to this path.")
    ap.add_argument("--is-threshold", type=int, default=8, help="Minimum calls required for a channel to be marked eligible (default: 8).")
    ap.add_argument("--from-date", default="", help="Optional YYYY-MM-DD filter on sent_at (inclusive)")
    ap.add_argument("--to-date", default="", help="Optional YYYY-MM-DD filter on sent_at (inclusive)")
    ap.add_argument("--time-frequency", default="month", choices=["day", "week", "month"])
    ap.add_argument("--llm-verify-enabled", action="store_true", help="Enable second-pass LLM verifier.")
    ap.add_argument("--llm-verify-endpoint", default=os.getenv("HERMES_LLM_VERIFY_ENDPOINT", "http://127.0.0.1:11434"), help="Ollama/llama.cpp server base URL; can also set HERMES_LLM_VERIFY_ENDPOINT for Predator")
    ap.add_argument("--llm-verify-model", default=os.getenv("HERMES_LLM_VERIFY_MODEL", "qwen2.5-7b-local"), help="Verifier model name; can also set HERMES_LLM_VERIFY_MODEL")
    ap.add_argument("--llm-verify-timeout-sec", type=int, default=15)
    ap.add_argument("--llm-verify-mode", default="review_only", choices=["review_only", "always"])
    ap.add_argument("--llm-extract-enabled", action="store_true", help="Enable OpenAI-compatible extraction fallback for noisy/partial call messages.")
    ap.add_argument("--llm-extract-endpoint", default=os.getenv("HERMES_LLM_EXTRACT_ENDPOINT", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")), help="OpenAI-compatible API base URL for extraction fallback.")
    ap.add_argument("--llm-extract-model", default=os.getenv("HERMES_LLM_EXTRACT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini")), help="OpenAI-compatible model name for extraction fallback.")
    ap.add_argument("--llm-extract-api-key", default=os.getenv("HERMES_LLM_EXTRACT_API_KEY", os.getenv("OPENAI_API_KEY", "")), help="API key for extraction fallback.")
    ap.add_argument("--llm-extract-timeout-sec", type=int, default=30)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    env = load_env(Path(args.env_file))
    api_id = env.get("TELEGRAM_API_ID")
    api_hash = env.get("TELEGRAM_API_HASH")
    session_path = env.get("TELEGRAM_SESSION_PATH", ".cache/telegram.session")
    if not api_id or not api_hash:
        raise SystemExit("Missing TELEGRAM_API_ID/TELEGRAM_API_HASH.")

    universe = cfg.get("universe") or []
    symbol_map: Dict[str, str] = {}
    symbol_sector_map: Dict[str, str] = {}
    for asset in universe:
        sym = str(asset.get("symbol") or "").upper().strip()
        if not sym:
            continue
        base = sym.split(".", 1)[0]
        symbol_map[base] = sym
        symbol_map[sym] = sym
        symbol_sector_map[sym] = str(asset.get("sector") or "unknown")

    sources = cfg.get("telegram", {}).get("sources") or []
    since = datetime.now(UTC) - timedelta(days=args.lookback_days)
    try:
        messages = asyncio.run(
            fetch_messages(
                int(api_id),
                api_hash,
                session_path,
                sources,
                since,
                int(args.max_messages_per_channel),
            )
        )
    except Exception as exc:
        raise SystemExit(f"Telegram fetch failed: {exc}") from exc
    parsed = parse_calls(
        messages,
        symbol_map,
        llm_verify_enabled=bool(args.llm_verify_enabled),
        llm_verify_endpoint=str(args.llm_verify_endpoint),
        llm_verify_model=str(args.llm_verify_model),
        llm_verify_timeout_sec=int(args.llm_verify_timeout_sec),
        llm_verify_mode=str(args.llm_verify_mode),
        llm_extract_enabled=bool(args.llm_extract_enabled),
        llm_extract_endpoint=str(args.llm_extract_endpoint),
        llm_extract_model=str(args.llm_extract_model),
        llm_extract_api_key=str(args.llm_extract_api_key),
        llm_extract_timeout_sec=int(args.llm_extract_timeout_sec),
    )

    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    now = datetime.now(UTC)

    bench_candles: List[Candle] | None = None
    try:
        bench_candles = fetch_candles(args.benchmark_symbol, args.market_data_range, "1d")
    except Exception:
        bench_candles = None

    candle_cache: Dict[str, List[Candle]] = {}
    outcomes: List[Dict[str, Any]] = []
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
                    candle_cache[call.symbol] = fetch_candles(call.symbol, args.market_data_range, "1d")
                except Exception:
                    continue
            call_outcomes = outcome_for_call(
                symbol=call.symbol,
                direction=call.direction,
                sent_at=sent,
                horizons=horizons,
                symbol_candles=candle_cache[call.symbol],
                benchmark_candles=bench_candles,
                win_thresh_pct=args.win_threshold_pct / 100.0,
                loss_thresh_pct=args.loss_threshold_pct / 100.0,
                intent=call.intent,
                entry_hint=call.entry_hint,
                stop_hint=call.stop_hint,
                target_hint=call.target_hint,
                target_hints=call.target_hints,
                trigger_above=call.trigger_above,
                trigger_below=call.trigger_below,
                prefer_target_stop=bool(args.prefer_target_stop),
                same_bar_policy=str(args.same_bar_policy),
            )
        for row in call_outcomes:
            outcomes.append(
                {
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
                }
            )

    if args.from_date or args.to_date:
        d_from = datetime.min.replace(tzinfo=UTC)
        d_to = datetime.max.replace(tzinfo=UTC)
        if args.from_date:
            d_from = datetime.fromisoformat(args.from_date).replace(tzinfo=UTC)
        if args.to_date:
            d_to = datetime.fromisoformat(args.to_date).replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
        filtered: List[Dict[str, Any]] = []
        for row in outcomes:
            dt = datetime.fromisoformat(str(row["sent_at_utc"]))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if d_from <= dt <= d_to:
                filtered.append(row)
        outcomes = filtered

    rankings = build_rankings(outcomes, now, args.half_life_days, symbol_sector_map)
    timeseries = build_time_series(outcomes, symbol_sector_map, args.time_frequency)
    metrics_v2 = compute_metrics_v2(outcomes, parsed_calls_count=len(parsed), message_count=len(messages))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": now.isoformat(),
        "lookback_days": args.lookback_days,
        "horizons_days": horizons,
        "message_count": len(messages),
        "parsed_calls_count": len(parsed),
        "outcome_rows_count": len(outcomes),
        "benchmark_symbol": args.benchmark_symbol,
        "market_data_range": args.market_data_range,
        "prefer_target_stop": bool(args.prefer_target_stop),
        "same_bar_policy": args.same_bar_policy,
        "win_threshold_pct": args.win_threshold_pct,
        "loss_threshold_pct": args.loss_threshold_pct,
        "rankings": rankings,
        "timeseries": timeseries,
        "metrics_v2": metrics_v2,
        "verifier": {
            "llm_enabled": bool(args.llm_verify_enabled),
            "llm_endpoint": args.llm_verify_endpoint,
            "llm_model": args.llm_verify_model,
            "llm_mode": args.llm_verify_mode,
            "llm_extract_enabled": bool(args.llm_extract_enabled),
            "llm_extract_endpoint": args.llm_extract_endpoint,
            "llm_extract_model": args.llm_extract_model,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out_dir / "messages.json").write_text(json.dumps(messages, indent=2), encoding="utf-8")
    (out_dir / "parsed_calls.json").write_text(json.dumps([c.__dict__ for c in parsed], indent=2), encoding="utf-8")
    (out_dir / "outcomes.json").write_text(json.dumps(outcomes, indent=2), encoding="utf-8")

    runtime_scores: Dict[str, Any] = {
        "generated_at_utc": now.isoformat(),
        "scoring_policy": "author_first_channel_only_if_single_poster",
        "channel_scores": {},
        "author_scores": {},
    }
    for row in rankings["channel_rankings"]:
        runtime_scores["channel_scores"][row["key"]] = {
            "score": row["recency_weighted_score"],
            "tier": row["tier"],
            "calls_count": row["calls_count"],
        }
    for row in rankings["author_rankings"]:
        runtime_scores["author_scores"][row["key"]] = {
            "score": row["recency_weighted_score"],
            "tier": row["tier"],
            "calls_count": row["calls_count"],
        }

    runtime_path = Path(args.runtime_json)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(json.dumps(runtime_scores, indent=2), encoding="utf-8")

    id_to_handle: Dict[str, str] = {}
    for item in outcomes:
        cid = str(item.get("channel_id") or "").strip()
        handle = str(item.get("channel_handle") or "").strip()
        if cid and handle:
            id_to_handle[cid] = handle

    if args.leaderboard_out:
        _write_leaderboard(payload, outcomes, id_to_handle, Path(args.leaderboard_out), now, is_threshold=args.is_threshold)

    print(
        json.dumps(
            {
                "out_dir": str(out_dir),
                "runtime_scores": str(runtime_path),
                "leaderboard": args.leaderboard_out or None,
                "message_count": len(messages),
                "parsed_calls_count": len(parsed),
                "outcome_rows_count": len(outcomes),
                "channels_ranked": len(rankings["channel_rankings"]),
                "authors_ranked": len(rankings["author_rankings"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
