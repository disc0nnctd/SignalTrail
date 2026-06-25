#!/usr/bin/env python3
"""Extract structured trade calls from Telegram messages with an LLM API key.

Input: JSON array of message objects.
Output: JSON array of extracted call objects.

The extractor is intentionally conservative:
- returns no-call when direction/levels are ambiguous
- keeps parser confidence explicit
- avoids generating speculative values
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SYSTEM_PROMPT = """You extract structured trading calls from Indian market Telegram messages.
Return strict JSON only with this schema:
{
  "is_call": boolean,
  "symbol": string|null,
  "direction": "bullish"|"bearish"|null,
  "entry": number|null,
  "target": number|null,
  "stop_loss": number|null,
  "call_type": "trade_plan"|"partial_plan"|"directional"|"non_actionable",
  "instrument_type": "equity"|"options"|"futures"|"index"|"unknown",
  "options_details": {
    "underlying": string|null,
    "strike": number|null,
    "option_type": "CE"|"PE"|null,
    "expiry": "weekly"|"monthly"|string|null
  }|null,
  "confidence": number,
  "reason": string
}
Rules:
- If ambiguous or non-actionable, set is_call=false and call_type=non_actionable.
- Do not invent levels not present in the text.
- Confidence is 0..1 and should drop for ambiguous language.
- For equity calls: symbol is the NSE ticker (e.g. RELIANCE, INFY, TCS).
- For options calls (containing CE/PE): set instrument_type=options, populate options_details with underlying index/stock, strike price, CE or PE, and expiry if mentioned. entry/target/stop_loss are the PREMIUM prices, not the underlying price.
- For index directional calls (NIFTY/BANKNIFTY without CE/PE): set instrument_type=index, symbol=NIFTY or BANKNIFTY, entry/target/stop_loss are index levels.
- For futures calls (FUT/futures mentioned): set instrument_type=futures.
- options_details is null for non-options calls.
"""


def _post_chat_completions(api_key: str, model: str, message: str, endpoint: str, timeout: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Message:\n{message}"},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    req = Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_extraction(raw: dict[str, Any]) -> dict[str, Any]:
    direction = raw.get("direction")
    if direction not in {"bullish", "bearish"}:
        direction = None
    call_type = raw.get("call_type")
    if call_type not in {"trade_plan", "partial_plan", "directional", "non_actionable"}:
        call_type = "non_actionable"
    conf = raw.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "is_call": bool(raw.get("is_call", False)),
        "symbol": (str(raw.get("symbol")).upper().strip() if raw.get("symbol") else None),
        "direction": direction,
        "entry": _to_float(raw.get("entry")),
        "target": _to_float(raw.get("target")),
        "stop_loss": _to_float(raw.get("stop_loss")),
        "call_type": call_type,
        "confidence": confidence,
        "reason": str(raw.get("reason") or ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM extractor for Telegram trade calls")
    ap.add_argument("--input", required=True, help="Input messages JSON array")
    ap.add_argument("--out", required=True, help="Output extracted calls JSON array")
    ap.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    ap.add_argument("--endpoint", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--timeout-sec", type=int, default=30)
    ap.add_argument("--sleep-ms", type=int, default=0, help="Optional delay between requests")
    args = ap.parse_args()

    if not args.api_key:
        raise SystemExit("Missing API key. Set OPENAI_API_KEY or pass --api-key.")

    src = Path(args.input)
    messages = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        raise SystemExit("Input must be a JSON array.")

    out_rows: list[dict[str, Any]] = []
    for msg in messages:
        text = str(msg.get("text") or msg.get("message") or "").strip()
        if not text:
            continue
        try:
            raw = _post_chat_completions(
                api_key=args.api_key,
                model=args.model,
                message=text,
                endpoint=args.endpoint,
                timeout=int(args.timeout_sec),
            )
        except (HTTPError, URLError, TimeoutError, OSError, KeyError, IndexError, json.JSONDecodeError) as exc:
            out_rows.append({
                "message_id": msg.get("message_id"),
                "channel": msg.get("channel_handle") or msg.get("channel") or None,
                "sent_at_utc": msg.get("sent_at_utc") or msg.get("date") or None,
                "text": text,
                "is_call": False,
                "symbol": None,
                "direction": None,
                "entry": None,
                "target": None,
                "stop_loss": None,
                "call_type": "non_actionable",
                "confidence": 0.0,
                "reason": f"llm_error:{type(exc).__name__}",
            })
            continue

        row = _normalize_extraction(raw)
        row.update({
            "message_id": msg.get("message_id"),
            "channel": msg.get("channel_handle") or msg.get("channel") or None,
            "sent_at_utc": msg.get("sent_at_utc") or msg.get("date") or None,
            "text": text,
        })
        out_rows.append(row)
        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_rows, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out_path), "rows": len(out_rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
