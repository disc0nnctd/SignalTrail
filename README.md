# SignalTrail

Transparency dashboard for evaluating historical performance of public Telegram market signal channels.
Fetches messages via Telegram MTProto, parses trading calls, scores them against real market data,
and publishes a masked public leaderboard.

> Not investment advice. Historical win rates do not predict future performance.

---

## Prerequisites

- Python 3.10+
- A Telegram account with API access (register at https://my.telegram.org/apps)
- Git + GitHub account (for the public Pages dashboard)
- Optional: Ollama (local LLM verifier) or an OpenAI-compatible API key (LLM extractor)

---

## Architecture

```
channels.json
    │
    ▼
scripts/evaluate.py          ← Telegram fetch + rule-based parser + scorer
    │   └─ (optional) scripts/extract_calls_llm.py  ← LLM extractor pass
    │
    ▼
data/output/
    ├── messages.json        ← raw Telegram messages (gitignored)
    ├── outcomes.json        ← per-call outcome rows
    ├── scores.json          ← per-channel scores
    └── summary.json         ← full ranked leaderboard
    │
    ▼
scripts/import-telegram-quality.py  ← masks identities, applies min-sample filter
    │
    ▼
public/leaderboard-public.json  ← committed with the static dashboard
    │
    ▼
public/                         ← static dashboard (HTML/JS/CSS)
```

signaltrail/ contains shared library code (market data fetching, scoring logic).
See [DEPLOY.md](DEPLOY.md) for the full deployment walkthrough.

---

## How it works

1. **Fetch** — `evaluate.py` connects to Telegram via your own MTProto session and pulls messages from the channels listed in `channels.json`.
2. **Parse** — A rule-based parser extracts directional calls (buy/sell), symbols, entry/stop/target levels. Optional LLM pass improves extraction on noisy messages.
3. **Score** — Each parsed call is evaluated against Yahoo Finance daily candles at 1d/3d/5d/10d horizons with benchmark-relative excess return.
4. **Publish** — `public/leaderboard-public.json` is written with identities masked and sanitized call excerpts for auditability.

---

> For a complete prerequisites + deployment walkthrough see [DEPLOY.md](DEPLOY.md).

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get Telegram API credentials

Create a Telegram application at https://my.telegram.org/apps and note the **API ID** and **API Hash**.

### 3. Create `.env`

```bash
cp .env.example .env
# fill in TELEGRAM_API_ID and TELEGRAM_API_HASH
```

### 4. One-time login

```bash
python3 scripts/login.py
```

Enter your phone number, OTP, and 2FA password when prompted.
The session file is saved to `.cache/telegram.session` (gitignored).

---

## Generate leaderboard

```bash
PYTHONPATH=. python3 scripts/evaluate.py
```

This writes:
- `public/leaderboard-public.json` — masked public leaderboard consumed by the static dashboard
- `data/output/summary.json` — full ranked output with all metrics
- `data/output/outcomes.json` — per-call outcome rows
- `data/output/scores.json` — lightweight scores per channel/author

### Common options

| Flag | Default | Description |
|------|---------|-------------|
| `--lookback-days` | 240 | How many days of messages to fetch |
| `--max-messages-per-channel` | 600 | Cap per channel |
| `--horizons` | `1,3,5,10` | Evaluation windows in trading days |
| `--benchmark-symbol` | `NIFTYBEES.NS` | Benchmark for excess return |
| `--leaderboard-out` | `public/leaderboard-public.json` | Output path for masked leaderboard |
| `--win-threshold-pct` | 1.0 | Min excess return % to count as win |
| `--loss-threshold-pct` | -1.0 | Max excess return % to count as loss |

---

## LLM-assisted parsing

Rule-based parsing misses calls in noisy or conversational messages.
Two optional LLM layers can improve results:

### Verifier (local, Ollama)

A lightweight model re-checks parsed calls and rejects false positives.

```bash
PYTHONPATH=. python3 scripts/evaluate.py \
  --llm-verify-enabled \
  --llm-verify-endpoint http://localhost:11434 \
  --llm-verify-model qwen2.5-7b-local \
  --llm-verify-mode review_only
```

`review_only` (default) only calls the LLM on messages the rule parser flagged as uncertain.
`always` runs the LLM on every accepted call — more accurate, slower.

**Recommended local models (via Ollama):** `qwen2.5-7b-local`, `gemma3:4b`, `phi4-mini`

### Extractor (OpenAI-compatible)

A stronger model extracts structured entry/stop/target levels from messages where the rule parser found only partial data.

```bash
PYTHONPATH=. python3 scripts/evaluate.py \
  --llm-extract-enabled \
  --llm-extract-endpoint https://api.openai.com/v1 \
  --llm-extract-model gpt-4.1-mini \
  --llm-extract-api-key $OPENAI_API_KEY
```

You can point `--llm-extract-endpoint` at any OpenAI-compatible server (local llama.cpp, vLLM, etc.).

### Environment variable shortcuts

```bash
export HERMES_LLM_VERIFY_ENDPOINT=http://localhost:11434
export HERMES_LLM_VERIFY_MODEL=qwen2.5-7b-local
export HERMES_LLM_EXTRACT_ENDPOINT=https://api.openai.com/v1
export HERMES_LLM_EXTRACT_MODEL=gpt-4.1-mini
export HERMES_LLM_EXTRACT_API_KEY=sk-...
```

---

## Adding channels

Edit `channels.json`:

```json
{
  "telegram": {
    "sources": [
      { "handle": "yourchannel", "label": "Your Channel" }
    ]
  },
  "universe": [...]
}
```

- `handle` is the Telegram public username (no `@`)
- `universe` is the list of NSE/BSE symbols to match against. Pre-populated from NSE equity list.

---

## GitHub Pages deployment

The static dashboard lives in `public/`. The included workflow (`.github/workflows/pages.yml`)
deploys the `public/` directory automatically on every push to `main`.

1. Run `evaluate.py` or `run_pipeline.sh`, then commit `public/leaderboard-public.json` with the rest of `public/`
2. Go to repo **Settings → Pages → Source: GitHub Actions**
3. The site goes live at `https://<username>.github.io/<repo>`

> If you prefer a manual branch-based setup, set source to **Deploy from branch → main / `public`**
> (not root — the HTML files live under `public/`).

---

## Disclaimer

- SignalTrail is a data transparency experiment, not a financial product.
- Performance metrics are historical and based on rule-parsed signals, not verified human intent.
- Not affiliated with SEBI, NSE, or any brokerage.
- If you deploy publicly, display sample-size confidence labels and link to methodology.
