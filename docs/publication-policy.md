# Publication Policy

SignalTrail is a public-interest analytics project. It is not an investment advisory product.

## Core Legal-Safety Posture

- Experimental data analysis only.
- Historical performance analytics only.
- No personalized recommendation output.
- Neutral and evidence-based language only.

## Mandatory Public Disclosures

Each public page should clearly state:

1. Not investment advice.
2. Historical performance does not guarantee future returns.
3. Data/model parsing may contain errors.
4. Dataset is experimental and for transparency research.

## Prohibited Language

Do not publish claims such as:

- scammer
- fraudster
- cheater
- intent-based allegations without legal findings

Use neutral alternatives:

- insufficient sample
- historically weak under this methodology
- mixed historical evidence

## Correction Channel (Required Before Public Launch)

A correction or dispute contact must be visible on every public page before launch.
Options (choose one and update the dashboard footer accordingly):

- GitHub Issues on the public repo (recommended for open-source deployments)
- A dedicated email address (e.g. `signaltrail-corrections@yourdomain.com`)
- A public feedback form

**Human decision required:** the correction channel has not been configured yet.
Update `public/index.html` with the chosen contact before launch.

## Pre-Publication Checklist

Before any public release:

1. Source rights: only public/permissioned content.
2. Privacy: no personal addresses, numbers, or sensitive identifiers.
3. Methodology: linked and current.
4. Confidence labels: visible (`IS` for insufficient sample).
5. Correction channel: visible and actionable (see section above — **not yet configured**).
6. Secondary moderation review: completed (**human decision required**).
7. Public artifact check: if you do not want to publish raw source labels, export a masked public dataset instead of your local truth dataset.

If any item fails, do not publish.

## Channel Selection and Removal

### Inclusion criteria

A channel may be included in `channels.json` if it meets all of the following:

- The channel is **publicly accessible** on Telegram (no invite required, no paywall)
- The channel posts content that can reasonably be interpreted as market directional calls
- The operator has not sent a documented removal request

### Removal criteria

A channel must be removed from the dataset and from any published leaderboard if:

- The channel goes private or is deleted
- The channel operator sends a documented removal/opt-out request to the correction channel
- A human reviewer finds the channel is publishing content under a false identity or fabricating call history

Removed channels are excluded from future evaluations. Historical data already committed is not retroactively purged unless a legal obligation requires it.

### Masked channels

Channels not designated as `approved_direct` in `channels.json` appear on the public leaderboard as `****`. Their handles and labels are never committed to `leaderboard-public.json`. The source-of-truth `channels.json` file is committed to the repo; deployers who want a fully anonymous dataset should fork and remove labels before publishing.

## Privacy Considerations for Public Telegram Messages

Telegram channels operated in public mode make their content available without authentication. SignalTrail processes only:

- Message text
- Message timestamp
- Sender handle (channel-level, not individual user)

**Not processed:** phone numbers, user IDs, profile photos, private group messages, direct messages, or any content from invite-only channels.

Processed messages are stored in `data/output/messages.json` (gitignored by default). Do not commit this file — it contains raw message text that may include personal handles posted by channel operators.

If a channel operator believes their content has been incorrectly included or that removal is required, they should submit a request via the correction channel listed on the public dashboard.

## India/SEBI Risk Note

SignalTrail is designed to avoid presenting itself as a recommendation/advisory service.
If monetization, recommendations, or user-specific guidance is introduced, obtain legal review before launch to assess SEBI RA/IA exposure.

This document is an engineering policy baseline, not legal advice.
