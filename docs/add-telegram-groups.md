# Add Telegram Groups

This guide covers safe onboarding of Telegram groups/channels for SignalTrail.

## Inclusion Rules

Add a source only if:

1. The content is public or permissioned.
2. Calls are parseable (symbol, direction, and preferably entry/target/stop).
3. Source quality is consistent enough for repeatable scoring.
4. Attribution to channel/author is stable.

## Exclusion Rules

Do not ingest:

- private leaks or paid-room content without rights
- personal data channels
- channels dominated by non-actionable chatter
- unverifiable repost chains with unclear source ownership

## Integration Flow

1. Add candidate handles to your Telegram ingestion configuration.
2. Run Telegram backfill to generate `summary.json` and `report.json`.
3. Generate leaderboard JSON using the importer script.
4. Verify sample-size labels and neutral language.
5. Run publication checklist before public release.

## Candidate List

Use [popular-groups.json](/home/notdc/SignalTrail/data/popular-groups.json) as discovery input only.

Inclusion in this list is not endorsement.
