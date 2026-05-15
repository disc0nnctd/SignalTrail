from __future__ import annotations

import json
from functools import lru_cache
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

_SYMBOL_ALIASES_PATH = Path(__file__).with_name("symbol_aliases.json")


@lru_cache(maxsize=1)
def load_symbol_alias_records() -> Dict[str, List[Dict[str, str]]]:
    if not _SYMBOL_ALIASES_PATH.exists():
        return {}
    try:
        data = json.loads(_SYMBOL_ALIASES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    aliases: Dict[str, List[Dict[str, str]]] = {}
    for key, value in data.items():
        symbol = str(key).strip().upper()
        if not symbol:
            continue
        if isinstance(value, str):
            values: List[Any] = [value]
        elif isinstance(value, list):
            values = value
        else:
            continue
        cleaned: List[Dict[str, str]] = []
        for item in values:
            if isinstance(item, str):
                alias = str(item).strip().upper()
                if alias:
                    cleaned.append({"symbol": alias})
                continue
            if not isinstance(item, dict):
                continue
            alias = str(item.get("symbol") or item.get("alias") or "").strip().upper()
            if not alias:
                continue
            record: Dict[str, str] = {"symbol": alias}
            effective_date = str(item.get("effective_date") or item.get("effectiveFrom") or "").strip()
            if effective_date:
                record["effective_date"] = effective_date
            note = str(item.get("note") or "").strip()
            if note:
                record["note"] = note
            cleaned.append(record)
        if cleaned:
            aliases[symbol] = cleaned
    return aliases


@lru_cache(maxsize=1)
def load_symbol_aliases() -> Dict[str, List[str]]:
    aliases: Dict[str, List[str]] = {}
    for key, records in load_symbol_alias_records().items():
        aliases[key] = [record["symbol"] for record in records if record.get("symbol")]
    return aliases


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.split("T", 1)[0]).date()
    except ValueError:
        return None


def symbol_alias_candidates(symbol: str, as_of: date | datetime | None = None) -> List[str]:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    base = raw.split(".", 1)[0]
    as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
    candidates: List[str] = []
    alias_records = load_symbol_alias_records().get(base, [])
    for record in alias_records:
        alias = record.get("symbol", "").strip().upper()
        if not alias:
            continue
        effective_date = _parse_iso_date(record.get("effective_date"))
        if as_of_date is not None and effective_date is not None and effective_date > as_of_date:
            continue
        for candidate in [alias, raw, base]:
            item = str(candidate).strip().upper()
            if item and item not in candidates:
                candidates.append(item)
    for candidate in [raw, base]:
        item = str(candidate).strip().upper()
        if item and item not in candidates:
            candidates.append(item)
    return candidates
