# Contributing

Thanks for your interest in contributing to Outreach Starter.

## Ground rules

- **No secrets.** Never commit real API keys, tokens, passwords, `.env` files, or database files.  
  The pre-commit hook in `.githooks/pre-commit` will block most accidental leaks — enable it once per clone:
  ```bash
  git config core.hooksPath .githooks
  ```
- **No lead data or PII.** Databases, CSV exports, suppression lists, and log files are gitignored — keep them that way.
- **One logical change per PR.** Smaller PRs are easier to review and faster to merge.

## Getting started

```bash
git clone https://github.com/lovefrosty/outreach-starter.git
cd outreach-starter
cp .env.example .env          # fill in your own keys
git config core.hooksPath .githooks
```

## Making a change

1. Fork the repo and create a branch: `git checkout -b my-feature`
2. Make your changes. Keep functions under ~20 lines; add comments only where the *why* is non-obvious.
3. Test locally before opening a PR.
4. Open a pull request with a clear description of what changed and why.

## What's in scope

- Bug fixes and correctness improvements to the pipeline nodes (`pipeline/nodes/`)
- New pluggable source adapters, provider adapters, or analyzer modules
- Schema and contract improvements (`schema/`)
- Documentation improvements

## What's out of scope

- Company-specific templates, campaigns, or lead data (fill in `config/` placeholders yourself)
- Proprietary integrations that require credentials not documented in `.env.example`

## Code style

- Python: PEP 8. Run `ruff check .` before committing.
- Bash: `set -euo pipefail` at the top, quote all variables.
- No generated comments or boilerplate docstrings.

## Reporting issues

Open a GitHub issue with a clear description of the problem, steps to reproduce, and the relevant pipeline node or script.
