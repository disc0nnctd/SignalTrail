#!/usr/bin/env python3
"""Convert Telegram quality evaluation output into public leaderboard JSON.

Input:  data/output/summary.json  (default; override with --input)
Output: public/leaderboard-public.json  (default; override with --out)

This intentionally publishes masked aggregate metrics plus short sanitized call
excerpts for auditability. Do not publish raw private messages or personal data
without a separate review process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from collections import defaultdict


def pct(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) * 100.0, 1)
    except (TypeError, ValueError):
        return None


def public_row_key(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def codename(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6].upper()
    return f"Source-{digest}"


def public_message_excerpt(text: Any, limit: int = 220) -> str:
    cleaned = str(text or "").replace("\n", " ").strip()
    cleaned = re.sub(r"https?://\S+|t\.me/\S+", "[link]", cleaned, flags=re.I)
    cleaned = re.sub(r"@\w+", "[handle]", cleaned)
    cleaned = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "[email]", cleaned)
    cleaned = re.sub(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b", "[phone]", cleaned)
    return cleaned[:limit]


PUBLIC_CHANNEL_LABELS: dict[str, str] = {
    "motilaloswalofficial": "Motilal Oswal Official",
    "nirmalbangofficial": "Nirmal Bang Official",
    "Official_AngelOne": "Angel One",
}


def channel_label(handle: str) -> str:
    return PUBLIC_CHANNEL_LABELS.get(handle, codename(handle or "channel"))


def build_breakdown_samples(outcomes: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(outcomes, key=lambda row: float(row.get("net_return_pct") or 0.0), reverse=True):
        call_id = str(item.get("call_id") or "").strip()
        if not call_id or call_id in seen:
            continue
        seen.add(call_id)
        exit_reason = str(item.get("exit_reason") or "").strip().lower()
        samples.append({
            "author_alias": codename(str(item.get("author_id") or item.get("author_name") or "")),
            "channel_alias": channel_label(str(item.get("channel_handle") or "")),
            "symbol": item.get("display_symbol") or item.get("symbol"),
            "instrument_type": item.get("instrument_type"),
            "direction": item.get("direction"),
            "evaluation_window": item.get("evaluation_window"),
            "evaluation_method": item.get("evaluation_method"),
            "outcome": item.get("label"),
            "outcome_credit": item.get("outcome_credit"),
            "exit_reason": item.get("exit_reason"),
            "reached_target": exit_reason.startswith("target"),
            "reached_stop": exit_reason.startswith("stop"),
            "net_return_pct": item.get("net_return_pct"),
            "benchmark_excess_return_pct": item.get("benchmark_excess_return_pct"),
            "message_excerpt": public_message_excerpt(item.get("text")),
            "target_prices": item.get("target_prices"),
            "targets_hit_count": item.get("targets_hit_count"),
            "target_count": item.get("target_count"),
            "exclude_from_performance": item.get("exclude_from_performance"),
            "exclusion_reason": item.get("exclusion_reason"),
        })
        if len(samples) >= limit:
            break
    return samples


def outcome_author_key(item: dict[str, Any]) -> str:
    if item.get("author_id") is not None:
        return str(item.get("author_id"))
    return f"name:{item.get('author_name') or 'unknown'}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/output/summary.json")
    ap.add_argument("--outcomes", default="data/output/outcomes.json")
    ap.add_argument("--out", default="public/leaderboard-public.json")
    ap.add_argument("--min-public-calls", type=int, default=8)
    ap.add_argument("--mask-identities", action=argparse.BooleanOptionalAction, default=True, help="Mask trader/channel identities for public-facing JSON.")
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}. Run evaluate.py first to generate it.")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    outcomes_path = Path(args.outcomes)
    outcomes_payload = json.loads(outcomes_path.read_text(encoding="utf-8")) if outcomes_path.exists() else []

    id_to_handle: dict[str, str] = {}
    for item in outcomes_payload:
        cid = str(item.get("channel_id") or item.get("author_id") or "").strip()
        handle = str(item.get("channel_handle") or "").strip()
        if cid and handle:
            id_to_handle[cid] = handle

    target_stop_counts: dict[str, dict[str, int]] = defaultdict(lambda: {
        "target_hits": 0,
        "stop_hits": 0,
        "target_stop_rows": 0,
        "resolved_target_stop_rows": 0,
    })
    author_outcomes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    same_bar_ambiguous_count = 0
    for item in outcomes_payload:
        author_outcomes[outcome_author_key(item)].append(item)
        author_name_key = str(item.get("author_name") or "").strip()
        if author_name_key:
            author_outcomes[author_name_key].append(item)
        if item.get("same_bar_ambiguous"):
            same_bar_ambiguous_count += 1
        if str(item.get("evaluation_method") or "") != "target_stop":
            continue
        author_keys = {
            str(item.get("author_id") or "").strip(),
            str(item.get("author_name") or "").strip(),
        }
        author_keys = {k for k in author_keys if k}
        exit_reason = str(item.get("exit_reason") or "").strip().lower()
        for key in author_keys:
            agg = target_stop_counts[key]
            agg["target_stop_rows"] += 1
            if exit_reason.startswith("target"):
                agg["target_hits"] += 1
                agg["resolved_target_stop_rows"] += 1
            elif exit_reason.startswith("stop"):
                agg["stop_hits"] += 1
                agg["resolved_target_stop_rows"] += 1

    author_rows = payload.get("rankings", {}).get("author_rankings", [])
    rows = []
    drilldowns: dict[str, dict[str, Any]] = {}
    rank = 1
    for row in author_rows:
        metrics = row.get("metrics_v2") or {}
        call_metrics = metrics.get("call_level") or {}
        row_metrics = metrics.get("row_level") or {}
        method_metrics = metrics.get("methods") or {}
        directional_metrics = method_metrics.get("directional_horizon") or {}
        target_stop_metrics = method_metrics.get("target_stop") or {}
        calls = int(row.get("calls_count") or 0)
        rows_count = int(row.get("rows_count") or row_metrics.get("rows_count") or 0)
        resolved_calls = int(call_metrics.get("resolved_calls_count") or 0)
        key = str(row.get("key") or "")
        ts_counts = target_stop_counts.get(key) or target_stop_counts.get(str(row.get("author_name") or "")) or {
            "target_hits": 0,
            "stop_hits": 0,
            "target_stop_rows": 0,
            "resolved_target_stop_rows": 0,
        }
        resolved_ts = int(ts_counts["resolved_target_stop_rows"] or 0)
        target_hits = int(ts_counts["target_hits"] or 0)
        stop_hits = int(ts_counts["stop_hits"] or 0)
        public_tier = "IS" if calls < args.min_public_calls else str(row.get("tier") or "D")
        public_rank = None if public_tier == "IS" else rank
        if public_rank is not None:
            rank += 1
        raw_key = str(row.get("key") or "")
        raw_handle = id_to_handle.get(raw_key, raw_key)
        raw_name = str(row.get("display_name") or row.get("author_name") or raw_key or "Unknown")
        raw_channel = str(row.get("channel") or raw_handle or "aggregate")
        row_key = public_row_key(raw_key)
        display_name = codename(raw_key) if args.mask_identities else raw_name
        channel_name = channel_label(raw_handle) if args.mask_identities else raw_channel
        instrument_types = metrics.get("instrument_types") or {}
        symbol_metrics = metrics.get("symbols") or {}
        per_symbol = [
            {
                "symbol": symbol,
                "rows_count": block.get("rows_count"),
                "resolved_win_rate": block.get("resolved_win_rate"),
                "expectancy": block.get("expectancy"),
                "avg_return_pct": block.get("avg_return_pct", block.get("expectancy")),
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
            "display_name": display_name,
            "channel": channel_name,
            "identity_masked": bool(args.mask_identities),
            "tier": public_tier,
            "score": None if public_tier == "IS" else row.get("recency_weighted_score"),
            "calls_evaluated": calls,
            "rows_evaluated": rows_count,
            "resolved_calls": resolved_calls,
            "trade_plan_coverage_pct": pct(call_metrics.get("actionability_rate")),
            "call_win_rate": call_metrics.get("win_rate"),
            "resolved_win_rate": call_metrics.get("resolved_win_rate"),
            "row_win_rate": row_metrics.get("win_rate"),
            "row_resolved_win_rate": row_metrics.get("resolved_win_rate"),
            "benchmark_relative_win_rate": directional_metrics.get("benchmark_relative_win_rate"),
            "target_stop_win_rate": target_stop_metrics.get("target_stop_win_rate"),
            "target_hits": target_hits,
            "stop_hits": stop_hits,
            "target_stop_rows": int(ts_counts["target_stop_rows"] or 0),
            "resolved_target_stop_rows": resolved_ts,
            "target_hit_rate": (target_hits / resolved_ts) if resolved_ts else None,
            "stop_hit_rate": (stop_hits / resolved_ts) if resolved_ts else None,
            "bayes_win_rate": call_metrics.get("bayes_win_rate"),
            "avg_r": call_metrics.get("expectancy"),
            "median_r": call_metrics.get("median_return"),
            "avg_return_pct": call_metrics.get("avg_return_pct", call_metrics.get("expectancy")),
            "median_return_pct": call_metrics.get("median_return_pct", call_metrics.get("median_return")),
            "profit_factor": call_metrics.get("profit_factor"),
            "timeout_rate": call_metrics.get("flat_rate"),
            "duplicate_rate": call_metrics.get("duplicate_rate"),
            "last_call_date": None,
            "confidence": "insufficient_sample" if public_tier == "IS" else "eligible",
            "win_rate": call_metrics.get("resolved_win_rate"),
            "legacy_win_rate": row.get("win_rate"),
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
            "display_name": display_name,
            "channel": channel_name,
            "tier": public_tier,
            "confidence": row_payload["confidence"],
            "calls_evaluated": calls,
            "resolved_win_rate": row_payload["resolved_win_rate"],
            "target_stop_win_rate": row_payload["target_stop_win_rate"],
            "instrument_breakdown": instrument_types,
            "per_symbol_breakdown": per_symbol,
            "parsed_calls": [],
        }
        seen_calls: set[str] = set()
        for item in sorted(author_outcomes.get(raw_key, []), key=lambda r: str(r.get("sent_at_utc") or ""), reverse=True):
            call_id = str(item.get("call_id") or "").strip()
            if not call_id or call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            drilldowns[row_key]["parsed_calls"].append({
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
                "message_excerpt": public_message_excerpt(item.get("text")),
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
            })
            if len(drilldowns[row_key]["parsed_calls"]) >= 25:
                break

    out = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_generated_at_utc": payload.get("generated_at_utc"),
        "methodology_version": "0.3.0",
        "identity_policy": "masked_public_aliases" if args.mask_identities else "raw_source_labels",
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
            "is_threshold": args.min_public_calls,
            "sample_size_policy": f"Rows with fewer than {args.min_public_calls} scored calls are labelled IS.",
        },
        "metrics_v2": payload.get("metrics_v2"),
        "breakdown_samples": build_breakdown_samples(outcomes_payload),
        "drilldowns": drilldowns,
        "rows": rows,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out_path), "rows": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
