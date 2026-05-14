# SignalTrail

SignalTrail is an open-source transparency dashboard for evaluating historical performance of public Telegram market calls.

## Positioning

This project is a data experiment and public-interest analytics tool.

- Not investment advice.
- Not a buy/sell recommendation engine.
- Not an accusation platform.

SignalTrail publishes reproducible historical metrics with clear limitations.

## Legal-Safety Baseline

For public deployment:

- Use neutral wording only.
- Avoid allegations (fraud/scam intent).
- Use only public or permissioned content.
- Expose correction/takedown path.
- Display sample-size confidence labels.
- Keep methodology and policy linked from UI.

SEBI-facing note for India users:
- SignalTrail does not provide personalized recommendations.
- If your usage model expands into paid recommendations/research distribution, obtain legal review for RA/IA regulatory exposure before launch.

## Project Structure

- `public/` static app
- `public/leaderboard.json` leaderboard dataset
- `docs/methodology.md` metric/evaluation rules
- `docs/publication-policy.md` publication and moderation policy
- `docs/add-telegram-groups.md` how to onboard Telegram groups
- `data/popular-groups.json` curated candidate list
- `scripts/import-telegram-quality.py` import upstream evaluation output

## Quick Start

```bash
cd /home/notdc/SignalTrail
python3 -m http.server 8791
```

Open `http://127.0.0.1:8791/index.html`.

## Refresh Data

```bash
python3 /home/notdc/SignalTrail/scripts/import-telegram-quality.py \
  --input /home/notdc/trader/reports-swing/telegram-quality/summary.json \
  --outcomes /home/notdc/trader/reports-swing/telegram-quality/outcomes.json \
  --out /home/notdc/SignalTrail/public/leaderboard.json
```

This importer now publishes explicit `target_hits` and `stop_hits` per author, derived from target/stop evaluated rows.

By default, local exports preserve real source labels.
Do not keep those local-truth files in the published root if the repo is going to GitHub Pages.
Use `data/private/` or another non-published location for internal review artifacts.

For public-facing exports or screenshots, generate a masked dataset:

```bash
python3 /home/notdc/SignalTrail/scripts/import-telegram-quality.py \
  --input /home/notdc/trader/reports-swing/telegram-quality/summary.json \
  --outcomes /home/notdc/trader/reports-swing/telegram-quality/outcomes.json \
  --out /home/notdc/SignalTrail/public/leaderboard-public.json \
  --mask-identities
```

The GitHub Pages / public site should publish only `leaderboard-public.json` at the repo root.


## Telegram API Login and Channel Access

Telegram access is handled by the internal trader, not the public site.

1. Create Telegram API credentials at `https://my.telegram.org/apps`
2. Run the one-time MTProto login helper:

```bash
python3 /home/notdc/trader/scripts/telegram_login.py
```

3. Enter your phone number, OTP, and 2FA password if prompted
4. The script saves a reusable session file so the trader can access public or permissioned channels without repeating the login flow

Notes:

- This uses Telegram MTProto via Telethon, not the Bot API.
- Only public or permissioned channels should be added to the trader configuration.
- Keep API ID, API hash, and session files out of public output.

## GitHub Pages Deployment

This repo is now root-layout friendly for GitHub Pages.

Public deployment rule:

- publish only the masked file: `leaderboard-public.json`
- keep local truth data outside the published root

After you push:

1. Open GitHub repo `Settings -> Pages`
2. Set `Source` to `GitHub Actions`
3. Push to `main`

## LLM Extraction (Target/SL Parsing)

If you want better extraction of target and stop loss from noisy chat text:

```bash
export OPENAI_API_KEY="..."
python3 /home/notdc/SignalTrail/scripts/extract_calls_llm.py \
  --input /path/to/messages.json \
  --out /path/to/extracted-calls.json \
  --model gpt-4.1-mini
```

This script returns structured fields (`symbol`, `direction`, `entry`, `target`, `stop_loss`, `confidence`) and marks ambiguous rows as non-actionable.

## Add Telegram Groups

Follow [docs/add-telegram-groups.md](/home/notdc/SignalTrail/docs/add-telegram-groups.md).

## License

MIT (see [LICENSE](/home/notdc/SignalTrail/LICENSE)).
