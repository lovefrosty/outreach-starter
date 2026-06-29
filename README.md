# Outreach Starter

A starter **cold-outreach engine** template: source businesses, discover & verify
contact emails, qualify and personalize with an LLM, send through a pluggable
provider (Instantly or Gmail) behind hard safety gates, and triage replies — all
driven by a SQLite system-of-record and operated from a Telegram command surface.

> This is a **de-branded template**. It ships no secrets, no databases, no lead
> data, and no company-specific content — only the engine skeleton and blank
> placeholder templates for you to fill in. Provide your own credentials via
> environment variables (see [`.env.example`](.env.example)).

## Pipeline

```
pulled -> scraped -> analyzed -> verified -> personalized -> queued -> sent
                                                  \ skipped / dead / call_list
```

| Node (`pipeline/nodes/`) | Role |
|------|------|
| c1_puller | Source businesses (Google Places / Apify Maps), ICP gate, dedupe, insert |
| c2_scraper | Scrape the business website for a contact email + signals |
| c3_analyzer | Qualify the lead (propensity, signals) |
| c4_guesser | Rank email candidates: on-site -> Hunter -> pattern guess |
| c5_verifier | Deliverability gate (MX / high-confidence on-site only) |
| c6_personalizer | Generate the personalized opener/body from templates |
| c7_sender | Send approved leads via provider; double-gated; routes by sequence_key |
| c8 / c9 | Classify reply intent and draft responses for human approval |

## Safety model

- **Two-key sending:** a lead sends only when human-approved (`queued`) **and** `PIPELINE_SENDING_ENABLED=1`.
- **Verification gate:** unverified / low-confidence emails never enter the send queue.
- **Suppression & dedupe** before send. **Provider abstraction** (`instantly` / `gmail`).
- **Secret guards:** `.gitignore`, a committed `.githooks/pre-commit` scan, and a gitleaks CI workflow.

## Layout

```
pipeline/   Pipeline nodes (c1-c9), sources/, providers/, orchestrator, ledger, control modules
scripts/    Operator bot, lead_store, enrichment helpers, entrypoints
config/     Non-secret policy + placeholder templates (fill these in)
schema/     Data/contract schemas
```

## Setup

```bash
cp .env.example .env            # fill in your own keys (never commit real values)
git config core.hooksPath .githooks   # enable the local secret guard
# brew install gitleaks               # optional: full local + CI secret scanning

python3 pipeline/daily_pipeline.py --limit 60   # source + advance (no send)
python3 pipeline/nodes/c7_sender.py --limit 15  # send approved, gate-enabled leads
```

See [`.env.example`](.env.example) for the full list of environment variables.

## Security & compliance

Secrets, databases, lead data, backups, and logs are excluded by `.gitignore` and
must never be committed. Sending is disabled by default behind explicit approval.
Cold outreach is subject to anti-spam law (CAN-SPAM, GDPR, CASL) and provider
terms — operate with a proper consent/legitimate-interest basis, honor
unsubscribes, and respect rate limits and robots.txt for any public-data sourcing.

## License

MIT — see [LICENSE](LICENSE).
