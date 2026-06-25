# Deployment Guide

## Prerequisites

- Python 3.10+
- A Telegram account with API access (MTProto credentials from https://my.telegram.org/apps)
- Git + a GitHub account (for the public Pages dashboard)

## Install

```bash
git clone <your-fork-url>
cd SignalAudit
pip install -r requirements.txt
```

## Configure environment

```bash
cp .env.example .env
# Edit .env — fill in TELEGRAM_API_ID and TELEGRAM_API_HASH
```

Required variables:

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | Integer API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | API hash string from my.telegram.org |
| `TELEGRAM_SESSION_PATH` | Where to store the session file (default: `.cache/telegram.session`) |

Optional variables (all also accepted as CLI flags to `evaluate.py`):

| Variable | Description |
|---|---|
| `HERMES_LLM_VERIFY_ENDPOINT` | Ollama/llama.cpp URL for local LLM verifier |
| `HERMES_LLM_VERIFY_MODEL` | Model name for local verifier (e.g. `qwen2.5-7b-local`) |
| `HERMES_LLM_EXTRACT_ENDPOINT` | OpenAI-compatible endpoint for LLM extractor |
| `HERMES_LLM_EXTRACT_MODEL` | Model name for extractor (e.g. `gpt-4.1-mini`) |
| `HERMES_LLM_EXTRACT_API_KEY` | API key for extractor endpoint |
| `HERMES_ALPHA_VANTAGE_API_KEY` | Alpha Vantage API key (optional candle source) |
| `HERMES_FYERS_ACCESS_TOKEN` | Fyers access token (optional candle source) |
| `SIGNALTRAIL_KEY_FILE` | Path to a supplementary key file that exports the above vars |

## One-time Telegram login

```bash
PYTHONPATH=. python3 scripts/login.py
```

Enter your phone number, the OTP Telegram sends you, and your 2FA password if set.
The session file is written to the path in `TELEGRAM_SESSION_PATH` and reused by all subsequent runs.

## Generate the leaderboard

```bash
PYTHONPATH=. python3 scripts/evaluate.py
```

This writes:
- `leaderboard-public.json` — masked public leaderboard (commit this for the dashboard)
- `data/output/summary.json` — full ranked output with all metrics
- `data/output/outcomes.json` — per-call outcome rows
- `data/output/scores.json` — lightweight scores per channel/author

### With LLM-assisted parsing (optional, improves accuracy)

Local verifier via Ollama:

```bash
PYTHONPATH=. python3 scripts/evaluate.py \
  --llm-verify-enabled \
  --llm-verify-endpoint http://localhost:11434 \
  --llm-verify-model qwen2.5-7b-local
```

Remote extractor via OpenAI-compatible API:

```bash
PYTHONPATH=. python3 scripts/evaluate.py \
  --llm-extract-enabled \
  --llm-extract-api-key "$HERMES_LLM_EXTRACT_API_KEY"
```

## Deploy the dashboard to GitHub Pages

1. Commit `leaderboard-public.json` (and the `public/` folder) to `main`
2. Push to GitHub
3. Go to **Settings → Pages → Source: GitHub Actions**
4. The workflow in `.github/workflows/pages.yml` deploys the `public/` directory automatically

The dashboard is live at `https://<username>.github.io/<repo>`.

## Adding channels

Edit `channels.json` and add entries under `telegram.sources`:

```json
{ "handle": "yourchannel", "label": "Your Channel" }
```

`handle` is the public Telegram username without the `@`.

## Running in production (scheduled)

The simplest approach is a cron job or systemd timer that runs `evaluate.py` daily and
then commits and pushes `leaderboard-public.json`:

```bash
# Example crontab entry (runs at 06:00 UTC)
0 6 * * * cd /path/to/SignalAudit && PYTHONPATH=. python3 scripts/evaluate.py && git add leaderboard-public.json && git commit -m "chore: update leaderboard" && git push
```

## First-run checklist

After completing the steps above, verify the following before treating the setup as working:

- [ ] `.env` exists and has non-placeholder values for `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`
- [ ] `.cache/telegram.session` was created by `login.py` (confirms auth succeeded)
- [ ] `evaluate.py` runs without error and writes `data/output/summary.json`
- [ ] `leaderboard-public.json` is written at repo root
- [ ] Opening `public/index.html` in a browser shows channels with data (not a blank table)
- [ ] `public/` methodology and publication-policy links resolve correctly

## GitHub Pages setup

1. Commit `leaderboard-public.json` and all `public/` files to `main`
2. Push to GitHub
3. In your repo: **Settings → Pages → Source: GitHub Actions**
4. The workflow at `.github/workflows/pages.yml` deploys `public/` on every push to `main`
5. Dashboard is live at `https://<username>.github.io/<repo>`

If you prefer branch-based deploy without Actions: **Settings → Pages → Deploy from branch → main / `public`** (select the `public` folder, not root).

## Pre-publication checklist

Before making the dashboard public, verify all items in `docs/publication-policy.md`:

- [ ] Only public or permissioned Telegram channels in `channels.json`
- [ ] `leaderboard-public.json` uses masked identities (this is the default)
- [ ] Methodology link visible on the dashboard
- [ ] Confidence labels (`IS` = insufficient sample) displayed
- [ ] A correction/contact channel is listed on the public page (GitHub Issues recommended)
- [ ] Secondary moderation review completed (human decision required — see policy doc)

## Debugging

All scripts print a JSON summary to stdout on success.  
Silent failures (market data fetch errors, candle gaps) are swallowed by design to avoid
aborting a full run over one bad symbol. Check `data/output/outcomes.json` for coverage —
symbols with zero outcome rows were likely skipped due to data fetch failures.

To debug without re-fetching Telegram messages, the cached `data/output/messages.json` from a
previous run can be parsed manually with the `parse_calls` function in `scripts/evaluate.py`.
