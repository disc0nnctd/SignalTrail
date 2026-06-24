from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .symbol_aliases import load_symbol_aliases, symbol_alias_candidates

YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
ALPHA_VANTAGE_BASE = "https://www.alphavantage.co/query"
FYERS_HISTORY_URLS = [
    "https://api-t1.fyers.in/data/history",
    "https://api.fyers.in/data-rest/v3/history",
]
USER_AGENT = "signaltrail/1.0"
_CANDLE_CACHE = Path(os.environ.get("SIGNALTRAIL_CACHE_DIR", Path(__file__).parent.parent / ".cache")) / "candle-cache.sqlite3"
_NSEMINE_SYMBOL_MAP_PATH = Path(os.environ.get("SIGNALTRAIL_CACHE_DIR", Path(__file__).parent.parent / ".cache")) / "nsemine-symbol-map.json"
_NSE_EQUITY_LIST_CACHE = Path(os.environ.get("SIGNALTRAIL_CACHE_DIR", Path(__file__).parent.parent / ".cache")) / "nse-equity-list.csv"
_NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

RANGE_TO_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "5y": 1825, "max": 3650,
}


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    candles: List[Candle]
    indicators: Dict[str, float | None]
    latest_price: float
    latest_volume: float


def safe_num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def average(values: List[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def sma(values: List[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> float | None:
    if len(values) < period:
        return None
    factor = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * factor + current * (1 - factor)
    return current


def rsi(values: List[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def ema_series(values: List[float], period: int) -> List[float] | None:
    if len(values) < period:
        return None
    factor = 2 / (period + 1)
    current = sum(values[:period]) / period
    series: List[float] = [current]
    for value in values[period:]:
        current = value * factor + current * (1 - factor)
        series.append(current)
    return series


def macd(values: List[float], fast: int, slow: int, signal: int) -> Dict[str, float | None]:
    if len(values) < slow + signal:
        return {"macd": None, "macd_signal": None, "macd_hist": None}
    fast_series = ema_series(values, fast)
    slow_series = ema_series(values, slow)
    if fast_series is None or slow_series is None:
        return {"macd": None, "macd_signal": None, "macd_hist": None}
    offset = slow - fast
    aligned_fast = fast_series[offset:]
    macd_line = [f - s for f, s in zip(aligned_fast, slow_series)]
    signal_series = ema_series(macd_line, signal)
    if signal_series is None:
        return {"macd": None, "macd_signal": None, "macd_hist": None}
    macd_now = macd_line[-1]
    signal_now = signal_series[-1]
    return {
        "macd": macd_now,
        "macd_signal": signal_now,
        "macd_hist": macd_now - signal_now,
    }


def atr(candles: List[Candle], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    true_ranges: List[float] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        true_ranges.append(true_range)
    if len(true_ranges) < period:
        return None
    return average(true_ranges[-period:])


def _return_over_lookback(values: List[float], lookback: int) -> float | None:
    if len(values) <= lookback:
        return None
    previous = values[-(lookback + 1)]
    if previous == 0:
        return None
    return (values[-1] / previous) - 1


def _sma_slope_pct(values: List[float], period: int, lookback: int = 20) -> float | None:
    if len(values) < period + lookback:
        return None
    now = sma(values, period)
    prev = sum(values[-(period + lookback):-lookback]) / period
    if now is None or prev == 0:
        return None
    return ((now - prev) / prev) * 100.0


def _classify_regime(indicators: Dict[str, float | None]) -> str:
    ema_trend = indicators.get("ema_trend")
    macd_hist = indicators.get("macd_hist")
    rsi_value = indicators.get("rsi")
    atr_pct = indicators.get("atr_pct")
    return_20d = indicators.get("return_20d")
    if None in (ema_trend, macd_hist, rsi_value, atr_pct, return_20d):
        return "mixed"
    if abs(float(return_20d)) < 0.02 and float(atr_pct) < 4.0:
        return "range"
    if float(ema_trend) > 0 and float(return_20d) > 0 and float(rsi_value) >= 52.0:
        return "bullish"
    if float(ema_trend) > 0 and float(return_20d) < 0 and float(rsi_value) <= 48.0:
        return "bearish"
    if macd_hist is not None and float(macd_hist) > 0 and float(rsi_value) >= 55.0:
        return "bullish"
    if macd_hist is not None and float(macd_hist) < 0 and float(rsi_value) <= 45.0:
        return "bearish"
    if float(atr_pct) >= 6.0:
        return "volatile"
    return "mixed"


def _merge_context_indicators(
    indicators: Dict[str, float | None],
    closes: List[float],
    benchmark_candles: List[Candle] | None = None,
    weekly_candles: List[Candle] | None = None,
    strategy_cfg: Dict[str, Any] | None = None,
) -> None:
    strategy_cfg = strategy_cfg or {}
    indicators["return_20d"] = _return_over_lookback(closes, 20)
    indicators["momentum_60"] = _return_over_lookback(closes, 60)
    if benchmark_candles:
        benchmark_closes = [candle.close for candle in benchmark_candles]
        indicators["benchmark_return_20d"] = _return_over_lookback(benchmark_closes, 20)
        indicators["benchmark_return_60d"] = _return_over_lookback(benchmark_closes, 60)
        if indicators["return_20d"] is not None and indicators["benchmark_return_20d"] is not None:
            indicators["relative_strength_20d"] = indicators["return_20d"] - indicators["benchmark_return_20d"]
        else:
            indicators["relative_strength_20d"] = None
        if indicators["momentum_60"] is not None and indicators["benchmark_return_60d"] is not None:
            indicators["relative_strength_60d"] = indicators["momentum_60"] - indicators["benchmark_return_60d"]
        else:
            indicators["relative_strength_60d"] = None
        if len(benchmark_closes) >= 21:
            indicators["benchmark_ema_fast"] = ema(benchmark_closes, int(strategy_cfg.get("ema_fast", 20)))
            indicators["benchmark_ema_slow"] = ema(benchmark_closes, int(strategy_cfg.get("ema_slow", 50)))
            indicators["benchmark_rsi"] = rsi(benchmark_closes, int(strategy_cfg.get("rsi_period", 14)))
            benchmark_macd = macd(
                benchmark_closes,
                int(strategy_cfg.get("macd_fast", 12)),
                int(strategy_cfg.get("macd_slow", 26)),
                int(strategy_cfg.get("macd_signal", 9)),
            )
            indicators["benchmark_macd"] = benchmark_macd["macd"]
            indicators["benchmark_macd_signal"] = benchmark_macd["macd_signal"]
            indicators["benchmark_macd_hist"] = benchmark_macd["macd_hist"]
            indicators["benchmark_regime"] = _classify_regime(
                {
                    "ema_trend": indicators["benchmark_ema_slow"],
                    "macd_hist": indicators["benchmark_macd_hist"],
                    "rsi": indicators["benchmark_rsi"],
                    "atr_pct": None,
                    "return_20d": indicators["benchmark_return_20d"],
                }
            )
        else:
            indicators["benchmark_ema_fast"] = None
            indicators["benchmark_ema_slow"] = None
            indicators["benchmark_rsi"] = None
            indicators["benchmark_macd"] = None
            indicators["benchmark_macd_signal"] = None
            indicators["benchmark_macd_hist"] = None
            indicators["benchmark_regime"] = None
            indicators["relative_strength_20d"] = None
            indicators["relative_strength_60d"] = None
    if weekly_candles:
        weekly_closes = [candle.close for candle in weekly_candles]
        weekly_fast = int(strategy_cfg.get("weekly_ema_fast", 10))
        weekly_slow = int(strategy_cfg.get("weekly_ema_slow", 20))
        indicators["weekly_ema_fast"] = ema(weekly_closes, weekly_fast)
        indicators["weekly_ema_slow"] = ema(weekly_closes, weekly_slow)
        indicators["weekly_return_20"] = _return_over_lookback(weekly_closes, 20)
        indicators["weekly_trend_confirm"] = bool(
            len(weekly_closes) >= weekly_slow
            and indicators["weekly_ema_fast"] is not None
            and indicators["weekly_ema_slow"] is not None
            and indicators["weekly_ema_fast"] > indicators["weekly_ema_slow"]
            and weekly_closes[-1] > indicators["weekly_ema_slow"]
        )
    else:
        indicators["weekly_ema_fast"] = None
        indicators["weekly_ema_slow"] = None
        indicators["weekly_return_20"] = None
        indicators["weekly_trend_confirm"] = None
    indicators.setdefault("relative_strength_20d", None)
    indicators.setdefault("relative_strength_60d", None)
    indicators["regime"] = _classify_regime(indicators)


def _atr_series(candles: List[Candle], period: int) -> List[float]:
    true_ranges: List[float] = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        true_ranges.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    if len(true_ranges) < period:
        return []
    result: List[float] = []
    current_atr = sum(true_ranges[:period]) / period
    result.append(current_atr)
    for tr in true_ranges[period:]:
        current_atr = (current_atr * (period - 1) + tr) / period
        result.append(current_atr)
    return result


def build_nifty_regime(
    benchmark_candles: List[Candle],
    regime_filter_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    closes = [c.close for c in benchmark_candles]
    slope_lookback = int(regime_filter_cfg.get("lookback_days_slope", 20))
    min_slope_pct = float(regime_filter_cfg.get("min_ema200_slope_pct", 0.10))
    max_atr_pct = float(regime_filter_cfg.get("max_atr_percentile", 70))
    atr_window = int(regime_filter_cfg.get("atr_percentile_window", 252))

    if len(closes) < 200 + slope_lookback:
        return {"nifty_ema200_slope": None, "nifty_atr_percentile": None, "nifty_regime": "unknown"}

    ema200_now = ema(closes, 200)
    ema200_prev = ema(closes[:-slope_lookback], 200)
    if ema200_now is None or ema200_prev is None or ema200_prev == 0:
        return {"nifty_ema200_slope": None, "nifty_atr_percentile": None, "nifty_regime": "unknown"}
    slope_pct = (ema200_now - ema200_prev) / ema200_prev * 100

    atr_vals = _atr_series(benchmark_candles, 14)
    if len(atr_vals) < 2:
        atr_percentile = 50.0
    else:
        window = atr_vals[-atr_window:] if len(atr_vals) >= atr_window else atr_vals
        current_atr = atr_vals[-1]
        atr_percentile = sum(1 for a in window if a <= current_atr) / len(window) * 100

    if slope_pct >= min_slope_pct and atr_percentile < max_atr_pct:
        nifty_regime = "trending"
    elif slope_pct < -min_slope_pct:
        nifty_regime = "declining"
    else:
        nifty_regime = "ranging"

    return {
        "nifty_ema200_slope": round(slope_pct, 4),
        "nifty_atr_percentile": round(atr_percentile, 1),
        "nifty_regime": nifty_regime,
    }


_BHARAT_SESSION: Any = None  # shared session to avoid repeated cookie init


def _bharat_session() -> Any:
    global _BHARAT_SESSION
    if _BHARAT_SESSION is None:
        from Technical import NSE  # type: ignore[import-not-found]
        _BHARAT_SESSION = NSE()
    return _BHARAT_SESSION


def _cache_conn() -> sqlite3.Connection:
    _CANDLE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_CANDLE_CACHE))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS candles ("
        "symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, "
        "PRIMARY KEY (symbol, date))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS enrichment ("
        "symbol TEXT, date TEXT, data_json TEXT, "
        "PRIMARY KEY (symbol, date))"
    )
    conn.commit()
    return conn


def _load_cache(conn: sqlite3.Connection, symbol: str, start: datetime) -> List[Candle]:
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM candles "
        "WHERE symbol=? AND date>=? ORDER BY date",
        (symbol, start.strftime("%Y-%m-%d")),
    ).fetchall()
    candles = []
    for date_str, o, h, l, c, v in rows:
        ts = datetime.fromisoformat(date_str).replace(tzinfo=UTC)
        candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
    return candles


def _save_cache(conn: sqlite3.Connection, symbol: str, candles: List[Candle]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO candles (symbol, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        [(symbol, c.ts.strftime("%Y-%m-%d"), c.open, c.high, c.low, c.close, c.volume)
         for c in candles],
    )
    conn.commit()


def _nse_symbol(symbol: str) -> str:
    return symbol.split(".")[0]


def _nsemine_symbol_candidates(symbol: str, as_of: datetime | None = None) -> List[str]:
    base = _nse_symbol(symbol).upper()
    merged: Dict[str, List[str]] = dict(load_symbol_aliases())
    try:
        if _NSEMINE_SYMBOL_MAP_PATH.exists():
            data = json.loads(_NSEMINE_SYMBOL_MAP_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    key = str(k).strip().upper()
                    if not key:
                        continue
                    if isinstance(v, str):
                        vals = [v]
                    elif isinstance(v, list):
                        vals = [str(item) for item in v]
                    else:
                        continue
                    cleaned = [x.strip().upper() for x in vals if str(x).strip()]
                    if cleaned:
                        merged[key] = cleaned
    except Exception:
        pass
    candidates = merged.get(base, [base])
    for item in symbol_alias_candidates(symbol, as_of):
        if item not in candidates:
            candidates.append(item)
    # Auto-discovery: try searching official NSE equity master when mapping is missing/stale.
    discovered = _discover_nse_symbol_candidates(base)
    for item in discovered:
        if item not in candidates:
            candidates.append(item)
    # Guarantee the original raw symbol is always tried.
    if base not in candidates:
        candidates.append(base)
    # preserve order and uniqueness
    seen = set()
    ordered: List[str] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _load_json_symbol_map() -> Dict[str, List[str]]:
    if not _NSEMINE_SYMBOL_MAP_PATH.exists():
        return {}
    try:
        data = json.loads(_NSEMINE_SYMBOL_MAP_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: Dict[str, List[str]] = {}
        for k, v in data.items():
            key = str(k).strip().upper()
            if not key:
                continue
            vals = [v] if isinstance(v, str) else (v if isinstance(v, list) else [])
            cleaned = [str(x).strip().upper() for x in vals if str(x).strip()]
            if cleaned:
                out[key] = cleaned
        return out
    except Exception:
        return {}


def _save_json_symbol_map(mapping: Dict[str, List[str]]) -> None:
    _NSEMINE_SYMBOL_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _NSEMINE_SYMBOL_MAP_PATH.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remember_nsemine_mapping(base: str, candidates: List[str]) -> None:
    base = base.strip().upper()
    if not base or not candidates:
        return
    current = _load_json_symbol_map()
    existing = current.get(base, [])
    merged: List[str] = []
    for item in [*candidates, *existing, base]:
        item_u = str(item).strip().upper()
        if item_u and item_u not in merged:
            merged.append(item_u)
    current[base] = merged
    try:
        _save_json_symbol_map(current)
    except Exception:
        pass


def _normalize_symbol_key(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _discover_candidates_from_rows(base: str, rows: List[Dict[str, str]]) -> List[str]:
    base_key = _normalize_symbol_key(base)
    if not base_key:
        return []
    ranked: List[tuple[int, str]] = []
    for row in rows:
        sym = str(row.get("SYMBOL") or "").strip().upper()
        if not sym:
            continue
        key = _normalize_symbol_key(sym)
        if not key:
            continue
        score = None
        if key == base_key:
            score = 0
        elif key.startswith(base_key) or base_key.startswith(key):
            score = 1
        elif base_key in key:
            score = 2
        if score is not None:
            ranked.append((score, sym))
    ranked.sort(key=lambda x: (x[0], len(x[1]), x[1]))
    out: List[str] = []
    seen = set()
    for _score, sym in ranked[:12]:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _load_nse_equity_rows() -> List[Dict[str, str]]:
    now = datetime.now(UTC)
    refresh = True
    if _NSE_EQUITY_LIST_CACHE.exists():
        age_hours = (now.timestamp() - _NSE_EQUITY_LIST_CACHE.stat().st_mtime) / 3600.0
        refresh = age_hours > 24.0
    if refresh:
        try:
            req = Request(_NSE_EQUITY_LIST_URL, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=20) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            _NSE_EQUITY_LIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _NSE_EQUITY_LIST_CACHE.write_text(data, encoding="utf-8")
        except Exception:
            pass
    if not _NSE_EQUITY_LIST_CACHE.exists():
        return []
    text = _NSE_EQUITY_LIST_CACHE.read_text(encoding="utf-8", errors="replace").splitlines()
    if not text:
        return []
    header = [h.strip() for h in text[0].split(",")]
    rows: List[Dict[str, str]] = []
    for line in text[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(header):
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def _discover_nse_symbol_candidates(base: str) -> List[str]:
    try:
        rows = _load_nse_equity_rows()
        return _discover_candidates_from_rows(base, rows)
    except Exception:
        return []


def _fetch_nsemine(symbol: str, start: datetime, end: datetime) -> List[Candle]:
    from nsemine import historical  # optional dep — imported lazily
    last_err: Exception | None = None
    base = _nse_symbol(symbol).upper()
    tried = _nsemine_symbol_candidates(symbol, as_of=end)
    for candidate in tried:
        try:
            df = historical.get_stock_historical_data(
                candidate, start_datetime=start, end_datetime=end, interval="D"
            )
            if df is None or df.empty:
                continue
            candles = []
            for _, row in df.iterrows():
                date_str = str(row["datetime"]).split(" ")[0]
                ts = datetime.fromisoformat(date_str).replace(tzinfo=UTC)
                o, h, l, c, v = (safe_num(row[k]) for k in ("open", "high", "low", "close", "volume"))
                if None in (o, h, l, c, v):
                    continue
                candles.append(Candle(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v)))
            if candles:
                # Self-heal mapping store when non-primary/rediscovered candidate works.
                if candidate != base:
                    _remember_nsemine_mapping(base, [candidate, base])
                return sorted(candles, key=lambda x: x.ts)
        except Exception as err:
            last_err = err
            continue
    # Self-heal attempt: if lookup failed, persist discovered candidates so next run tries them first.
    discovered = _discover_nse_symbol_candidates(base)
    if discovered:
        _remember_nsemine_mapping(base, discovered + [base])
    if last_err:
        raise RuntimeError(f"nsemine failed for {symbol}: {last_err}") from last_err
    raise RuntimeError(f"nsemine returned no data for {symbol}")


_INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}
_FYERS_ENV_LOADED = False


def _load_key_file_env_once() -> None:
    global _FYERS_ENV_LOADED
    if _FYERS_ENV_LOADED:
        return
    _FYERS_ENV_LOADED = True
    # Configurable via SIGNALTRAIL_KEY_FILE env var; no default path to avoid
    # loading credentials from unexpected locations on other machines.
    key_file_path = os.environ.get("SIGNALTRAIL_KEY_FILE", "")
    if not key_file_path:
        return
    key_file = Path(key_file_path)
    if not key_file.exists():
        return
    try:
        for raw in key_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            k = key.strip()
            if not k or k in os.environ:
                continue
            os.environ[k] = value.strip()
    except Exception:
        return


def _fyers_symbol(symbol: str) -> str:
    base = _nse_symbol(symbol).upper()
    return f"NSE:{base}-EQ"


def _fyers_resolution(interval: str) -> str | None:
    mapping = {
        "1m": "1",
        "2m": "2",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "90m": "60",
        "1h": "60",
        "1d": "D",
        "1wk": "W",
    }
    return mapping.get((interval or "").strip().lower())


def _fetch_fyers_history(symbol: str, start: datetime, end: datetime, interval: str, access_token: str) -> List[Candle]:
    if not access_token:
        raise RuntimeError("FYERS access token is not configured")
    resolution = _fyers_resolution(interval)
    if not resolution:
        raise RuntimeError(f"FYERS interval not supported: {interval}")
    payload = urlencode(
        {
            "symbol": _fyers_symbol(symbol),
            "resolution": resolution,
            "date_format": "0",
            "range_from": str(int(start.timestamp())),
            "range_to": str(int(end.timestamp())),
            "cont_flag": "1",
        }
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {access_token}",
    }
    last_error: Exception | None = None
    for base_url in FYERS_HISTORY_URLS:
        request = Request(f"{base_url}?{payload}", headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
            candles_raw = body.get("candles") or []
            candles: List[Candle] = []
            for item in candles_raw:
                if not isinstance(item, list) or len(item) < 6:
                    continue
                ts = datetime.fromtimestamp(int(item[0]), tz=UTC)
                o, h, l, c, v = (safe_num(item[i]) for i in range(1, 6))
                if None in (o, h, l, c, v):
                    continue
                candles.append(
                    Candle(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v))
                )
            if candles:
                return sorted(candles, key=lambda x: x.ts)
            msg = body.get("message") or body.get("s") or "no candles"
            last_error = RuntimeError(f"FYERS returned no data for {symbol}: {msg}")
        except Exception as err:
            last_error = err
            continue
    if last_error:
        raise RuntimeError(f"FYERS failed for {symbol}: {last_error}") from last_error
    raise RuntimeError(f"FYERS returned no data for {symbol}")


def _fetch_jugaad(symbol: str, start: datetime, end: datetime) -> List[Candle]:
    from jugaad_data.nse import stock_df  # optional dep — imported lazily
    from datetime import date as _date

    base = _nse_symbol(symbol).upper()
    from_date = _date(start.year, start.month, start.day)
    to_date = _date(end.year, end.month, end.day)
    df = stock_df(symbol=base, from_date=from_date, to_date=to_date, series="EQ")
    if df is None or df.empty:
        raise RuntimeError(f"jugaad returned no data for {symbol}")
    candles = []
    for _, row in df.iterrows():
        try:
            ts_raw = row["DATE"]
            if hasattr(ts_raw, "date"):
                ts = datetime(ts_raw.year, ts_raw.month, ts_raw.day, tzinfo=UTC)
            else:
                ts = datetime.fromisoformat(str(ts_raw).split(" ")[0]).replace(tzinfo=UTC)
            o, h, l, c, v = float(row["OPEN"]), float(row["HIGH"]), float(row["LOW"]), float(row["CLOSE"]), float(row["VOLUME"])
        except (KeyError, ValueError, TypeError):
            continue
        candles.append(Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v))
    if not candles:
        raise RuntimeError(f"jugaad returned unparseable data for {symbol}")
    return sorted(candles, key=lambda x: x.ts)


def _fetch_yahoo(symbol: str, data_range: str, interval: str, retries: int = 3) -> List[Candle]:
    query = urlencode({"range": data_range, "interval": interval, "includePrePost": "false"})
    request = Request(
        f"{YAHOO_CHART_BASE}/{symbol}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as error:
            if error.code == 429 or error.code >= 500:
                last_error = RuntimeError(f"Yahoo Finance HTTP {error.code} for {symbol}")
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Yahoo Finance HTTP {error.code} for {symbol}") from error
        except URLError as error:
            last_error = RuntimeError(f"Yahoo Finance network error for {symbol}: {error.reason}")
            time.sleep(2 ** attempt)
            continue
    else:
        raise last_error
    result = payload.get("chart", {}).get("result", [])
    if not result:
        error_text = payload.get("chart", {}).get("error") or "missing chart result"
        raise RuntimeError(f"No market data for {symbol}: {error_text}")
    data = result[0]
    timestamps = data.get("timestamp") or []
    quote = (data.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs, lows, closes, volumes = (
        quote.get(k) or [] for k in ("open", "high", "low", "close", "volume")
    )
    candles = []
    for index, ts in enumerate(timestamps):
        o = safe_num(opens[index] if index < len(opens) else None)
        h = safe_num(highs[index] if index < len(highs) else None)
        l = safe_num(lows[index] if index < len(lows) else None)
        c = safe_num(closes[index] if index < len(closes) else None)
        v = safe_num(volumes[index] if index < len(volumes) else None)
        if None in (o, h, l, c, v):
            continue
        candles.append(Candle(ts=datetime.fromtimestamp(int(ts), tz=UTC),
                              open=float(o), high=float(h), low=float(l),
                              close=float(c), volume=float(v)))
    return candles


def _alpha_vantage_symbol_candidates(symbol: str, as_of: datetime | None = None) -> List[str]:
    base = _nse_symbol(symbol).upper()
    candidates = [*symbol_alias_candidates(symbol, as_of), symbol, base, f"NSE:{base}", f"{base}.NSE", f"{base}.NS"]
    deduped: List[str] = []
    for candidate in candidates:
        item = str(candidate or "").strip()
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _fetch_alpha_vantage(symbol: str, start: datetime, end: datetime, api_key: str, retries: int = 3) -> List[Candle]:
    if not api_key:
        raise RuntimeError("Alpha Vantage API key is not configured")
    last_error: Exception | None = None
    for candidate in _alpha_vantage_symbol_candidates(symbol, as_of=end):
        query = urlencode(
            {
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": candidate,
                "outputsize": "full",
                "apikey": api_key,
            }
        )
        request = Request(f"{ALPHA_VANTAGE_BASE}?{query}", headers={"User-Agent": USER_AGENT})
        for attempt in range(retries):
            try:
                with urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except (HTTPError, URLError) as error:
                last_error = RuntimeError(f"Alpha Vantage network error for {candidate}: {error}")
                time.sleep(2 ** attempt)
                continue
        else:
            continue

        series = payload.get("Time Series (Daily)")
        if not isinstance(series, dict):
            note = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            if note:
                last_error = RuntimeError(f"Alpha Vantage rejected {candidate}: {note}")
            continue

        candles: List[Candle] = []
        for day, row in series.items():
            try:
                ts = datetime.fromisoformat(day).replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < start or ts > end + timedelta(days=1):
                continue
            o = safe_num((row or {}).get("1. open"))
            h = safe_num((row or {}).get("2. high"))
            l = safe_num((row or {}).get("3. low"))
            c = safe_num((row or {}).get("4. close"))
            v = safe_num((row or {}).get("6. volume"))
            if None in (o, h, l, c, v):
                continue
            candles.append(
                Candle(
                    ts=ts,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(v),
                )
            )
        if candles:
            return sorted(candles, key=lambda x: x.ts)
    if last_error:
        raise RuntimeError(f"Alpha Vantage failed for {symbol}: {last_error}") from last_error
    raise RuntimeError(f"Alpha Vantage returned no data for {symbol}")


def fetch_enrichment(symbol: str) -> Dict[str, Any]:
    """Return supplementary NSE data for symbol: P/E, delivery%, volatility, VWAP, 52w range, pre-open.

    Cached once per trading day. Returns empty dict on any failure — callers must handle gracefully.
    """
    nse_symbol = _nse_symbol(symbol)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    conn = _cache_conn()
    row = conn.execute(
        "SELECT data_json FROM enrichment WHERE symbol=? AND date=?", (symbol, today)
    ).fetchone()
    if row:
        conn.close()
        return json.loads(row[0])

    result: Dict[str, Any] = {}
    try:
        nse = _bharat_session()
        df = nse.get_trade_info(nse_symbol)
        if df is not None and not df.empty:
            row_data = df.iloc[0]
            def _col(col: str) -> Any:
                return row_data.get(col) if col in df.columns else None

            result = {
                "last_price":       safe_num(_col("priceInfo_lastPrice")),
                "vwap":             safe_num(_col("priceInfo_vwap")),
                "prev_close":       safe_num(_col("priceInfo_previousClose")),
                "week52_high":      safe_num(_col("priceInfo_weekHighLow_max")),
                "week52_low":       safe_num(_col("priceInfo_weekHighLow_min")),
                "pe_ratio":         safe_num(_col("metadata_pdSymbolPe")),
                "sector_pe":        safe_num(_col("metadata_pdSectorPe")),
                "delivery_pct":     safe_num(_col("securityWiseDP_deliveryToTradedQuantity")),
                "daily_volatility": safe_num(_col("marketDeptOrderBook_tradeInfo_cmDailyVolatility")),
                "annual_volatility":safe_num(_col("marketDeptOrderBook_tradeInfo_cmAnnualVolatility")),
                "upper_circuit":    safe_num(_col("priceInfo_upperCP")),
                "lower_circuit":    safe_num(_col("priceInfo_lowerCP")),
                "preopen_iep":      safe_num(_col("preOpenMarket_IEP")),
                "preopen_pct":      safe_num(_col("preOpenMarket_perChange")),
                "market_cap":       safe_num(_col("marketDeptOrderBook_tradeInfo_totalMarketCap")),
                "impact_cost":      safe_num(_col("marketDeptOrderBook_tradeInfo_impactCost")),
                "sector":           str(_col("industryInfo_sector") or ""),
                "industry":         str(_col("industryInfo_industry") or ""),
            }
            # Remove None values to keep prompt compact
            result = {k: v for k, v in result.items() if v is not None and v != ""}
            conn.execute(
                "INSERT OR REPLACE INTO enrichment (symbol, date, data_json) VALUES (?,?,?)",
                (symbol, today, json.dumps(result)),
            )
            conn.commit()
    except Exception:
        pass  # enrichment is best-effort — never block trading
    conn.close()
    return result


def fetch_candles(
    symbol: str,
    data_range: str,
    interval: str,
    retries: int = 3,
    force_refresh: bool = False,
    market_cfg: Dict[str, Any] | None = None,
) -> List[Candle]:
    days = RANGE_TO_DAYS.get(data_range, 365)
    start = datetime.now(UTC) - timedelta(days=days)
    end = datetime.now(UTC)

    conn = _cache_conn()
    cached = _load_cache(conn, symbol, start)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    cache_has_today = any(c.ts.strftime("%Y-%m-%d") >= today for c in cached) if cached else False
    # Use cache if we have enough history and it includes recent data (within 2 trading days)
    recent_cutoff = (datetime.now(UTC) - timedelta(days=4)).strftime("%Y-%m-%d")
    cache_fresh = cached and any(c.ts.strftime("%Y-%m-%d") >= recent_cutoff for c in cached)
    if not force_refresh and cache_fresh and len(cached) >= 30:
        conn.close()
        return cached

    # For intraday intervals use Yahoo Finance directly; for daily use jugaad → nsemine → Yahoo
    candles: List[Candle] = []
    errors: List[str] = []
    _load_key_file_env_once()
    fyers_key = (
        os.environ.get("HERMES_FYERS_ACCESS_TOKEN")
        or os.environ.get("FYERS_ACCESS_TOKEN")
        or str(((market_cfg or {}).get("fyers") or {}).get("access_token") or "").strip()
    )
    if interval in _INTRADAY_INTERVALS:
        sources = [
            lambda: _fetch_fyers_history(symbol, start, end, interval, fyers_key),
            lambda: _fetch_yahoo(symbol, data_range, interval, retries),
        ]
    else:
        market_cfg = market_cfg or {}
        alpha_key = (
            os.environ.get("HERMES_ALPHA_VANTAGE_API_KEY")
            or os.environ.get("ALPHA_VANTAGE_API_KEY")
            or str((market_cfg.get("alpha_vantage") or {}).get("api_key") or "").strip()
        )
        sources = [
            lambda: _fetch_fyers_history(symbol, start, end, interval, fyers_key),
            lambda: _fetch_jugaad(symbol, start, end),
            lambda: _fetch_nsemine(symbol, start, end),
            lambda: _fetch_alpha_vantage(symbol, start, end, alpha_key, retries),
            lambda: _fetch_yahoo(symbol, data_range, interval, retries),
        ]
    for source in sources:
        try:
            candles = source()
            if candles:
                break
        except Exception as err:
            errors.append(str(err))
    if not candles:
        conn.close()
        if cached and len(cached) >= 30:
            return cached  # serve stale cache rather than fail hard
        raise RuntimeError(f"All data sources failed for {symbol}: {'; '.join(errors)}")

    if candles:
        _save_cache(conn, symbol, candles)
    conn.close()

    if len(candles) < 30:
        raise RuntimeError(f"Insufficient candle history for {symbol}: {len(candles)} bars")
    return candles


def build_snapshot(
    symbol: str,
    candles: List[Candle],
    strategy_cfg: Dict[str, Any],
    benchmark_candles: List[Candle] | None = None,
    weekly_candles: List[Candle] | None = None,
) -> MarketSnapshot:
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    latest_price = closes[-1]
    atr_value = atr(candles, int(strategy_cfg["atr_period"]))
    atr_pct = (atr_value / latest_price * 100) if atr_value and latest_price else None
    indicators: Dict[str, float | None] = {
        "ema_fast": ema(closes, int(strategy_cfg["ema_fast"])),
        "ema_slow": ema(closes, int(strategy_cfg["ema_slow"])),
        "rsi": rsi(closes, int(strategy_cfg["rsi_period"])),
        "atr": atr_value,
        "atr_pct": atr_pct,
        "avg_volume_20": average(volumes[-20:]),
        "avg_volume_5": average(volumes[-5:]),
        "momentum_3": ((closes[-1] / closes[-4]) - 1) if len(closes) >= 4 else None,
        "momentum_10": ((closes[-1] / closes[-11]) - 1) if len(closes) >= 11 else None,
        "dma_50": sma(closes, 50),
        "dma_100": sma(closes, 100),
        "dma_200": sma(closes, 200),
        "dma_50_slope_pct": _sma_slope_pct(closes, 50),
        "dma_200_slope_pct": _sma_slope_pct(closes, 200),
    }
    if "ema_trend" in strategy_cfg:
        indicators["ema_trend"] = ema(closes, int(strategy_cfg["ema_trend"]))
    macd_fast = int(strategy_cfg.get("macd_fast", 12))
    macd_slow = int(strategy_cfg.get("macd_slow", 26))
    macd_signal = int(strategy_cfg.get("macd_signal", 9))
    indicators.update(macd(closes, macd_fast, macd_slow, macd_signal))
    _merge_context_indicators(indicators, closes, benchmark_candles, weekly_candles, strategy_cfg)
    return MarketSnapshot(
        symbol=symbol,
        candles=candles,
        indicators=indicators,
        latest_price=latest_price,
        latest_volume=volumes[-1],
    )


def fetch_market_snapshot(
    symbol: str,
    market_cfg: Dict[str, Any],
    strategy_cfg: Dict[str, Any],
    benchmark_candles: List[Candle] | None = None,
    weekly_candles: List[Candle] | None = None,
) -> MarketSnapshot:
    candles = fetch_candles(symbol, market_cfg["data_range"], market_cfg["data_interval"], market_cfg=market_cfg)
    return build_snapshot(
        symbol,
        candles,
        strategy_cfg,
        benchmark_candles=benchmark_candles,
        weekly_candles=weekly_candles,
    )
