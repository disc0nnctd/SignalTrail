#!/usr/bin/env bash
# run_pipeline.sh - full SignalAudit pipeline
#
# Stages:
#   1. evaluate.py        - fetch Telegram messages, score calls, write data/output/ and the public leaderboard
#   2. extract_calls_llm.py (optional, set LLM_EXTRACT=1) - LLM re-extraction pass for audit experiments
#   3. import-telegram-quality.py (legacy only, set LEGACY_IMPORT=1)
#
# Environment variables (all optional, override defaults):
#   EVALUATE_ARGS         extra args forwarded to evaluate.py
#   LLM_EXTRACT           set to 1 to run the optional LLM extraction stage
#   EXTRACT_ARGS          extra args forwarded to extract_calls_llm.py
#   LEGACY_IMPORT         set to 1 to overwrite the leaderboard with the legacy importer
#   IMPORT_ARGS           extra args forwarded to import-telegram-quality.py when LEGACY_IMPORT=1
#   OUT_DIR               data output dir (default: data/output)
#   LEADERBOARD_OUT       leaderboard output path (default: public/leaderboard-public.json)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

OUT_DIR="${OUT_DIR:-data/output}"
LEADERBOARD_OUT="${LEADERBOARD_OUT:-public/leaderboard-public.json}"

echo "[1/3] Running evaluate.py → $OUT_DIR and $LEADERBOARD_OUT ..."
python3 scripts/evaluate.py \
    --out-dir "$OUT_DIR" \
    --runtime-json "$OUT_DIR/scores.json" \
    --leaderboard-out "$LEADERBOARD_OUT" \
    ${EVALUATE_ARGS:-}

if [[ "${LLM_EXTRACT:-0}" == "1" ]]; then
    echo "[2/3] Running extract_calls_llm.py (LLM_EXTRACT=1) ..."
    python3 scripts/extract_calls_llm.py \
        --input "$OUT_DIR/messages.json" \
        --out "$OUT_DIR/extracted_calls.json" \
        ${EXTRACT_ARGS:-}
else
    echo "[2/3] Skipping extract_calls_llm.py (set LLM_EXTRACT=1 to enable)"
fi

if [[ "${LEGACY_IMPORT:-0}" == "1" ]]; then
    echo "[3/3] Running legacy import-telegram-quality.py → $LEADERBOARD_OUT ..."
    python3 scripts/import-telegram-quality.py \
        --input "$OUT_DIR/summary.json" \
        --outcomes "$OUT_DIR/outcomes.json" \
        --out "$LEADERBOARD_OUT" \
        ${IMPORT_ARGS:-}
else
    echo "[3/3] Skipping legacy importer (evaluate.py already wrote $LEADERBOARD_OUT)"
fi

echo "Pipeline complete. Leaderboard: $LEADERBOARD_OUT"
