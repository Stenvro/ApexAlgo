# Contributing to ApexAlgo

Thanks for taking an interest in ApexAlgo. This is a small project with a single maintainer, so every bug report, strategy example and pull request helps. This document explains how to make your contribution easy to review and merge.

## Reporting a bug

Open an issue at <https://github.com/Stenvro/ApexAlgo/issues> and include:

- **What you did, what you expected, what happened.**
- **Exchange, symbol(s) and timeframe** the bot was running on, and whether it was in backtest, paper or live mode.
- **The bot export** (`Export` on the bot card → `.apex.json`). Exports contain the strategy and settings only — never the API secret — but they do include the *name* of the linked exchange key, so review the file before attaching.
- **Logs**: the relevant lines from the bot's console output (toggle it on the bot card) and/or `docker compose logs backend | tail -50`.
- **Version**: the commit you are on (`git rev-parse --short HEAD`) and whether you run Docker or a manual install.

Never paste the contents of `data/.env`, exchange API keys or your database in an issue.

**Security vulnerabilities must not be reported as public issues** — see [SECURITY.md](SECURITY.md).

## Suggesting a feature

Open an issue describing the problem you want to solve, not only the solution you have in mind. If it touches strategy logic, a short description of the strategy (or an `.apex.json`) makes the request much more concrete.

## Development setup

The README covers everything you need: [Quick Start (Docker)](README.md#quick-start-docker) and [Development Workflow](README.md#development-workflow) (hot-reloading backend/frontend changes), plus [Manual Installation](README.md#manual-installation-without-docker) if you prefer running without Docker. The [Architecture](README.md#architecture) section explains how the pieces fit together.

Useful reference material while working on the engine or builder:

- [`STRATEGY_CONTEXT.md`](STRATEGY_CONTEXT.md) — the full node/indicator/settings schema of a bot.
- [`frontend/src/DESIGN.md`](frontend/src/DESIGN.md) — the UI design-system contract.

## Branching and pull requests

- `dev` is the working branch; `master` only receives releases. **Open pull requests against `dev`.**
- Fork the repository, create a branch from `dev` (`git checkout -b fix/short-description dev`), commit with a clear message (`fix:`, `feat:`, `perf:`, `docs:` prefixes are used throughout the history) and open a PR.
- Keep PRs focused. A bug fix and an unrelated refactor should be two PRs.
- Describe *how you tested* the change. Run `python -m pytest -q tests` (see README → Tests and lint); for engine changes the golden backtests must stay byte-identical, or the PR must say why a snapshot was regenerated (`UPDATE_GOLDEN=1`) and what changed in the trades.
- Don't commit anything from `data/` (database, `.env`, certificates) — `.gitignore` already blocks it, keep it that way.

## Code style

**Backend (Python 3.11+, FastAPI, SQLAlchemy)**
- Follow the surrounding code; no new dependencies without a good reason (every one is pinned in `requirements.txt`).
- `ruff check backend tests scripts` and `python -m pytest -q tests` must stay green (CI runs both). New engine behaviour needs a test next to the existing ones in `tests/`.
- Schema changes go through the idempotent migrations in `backend/core/database.py::run_migrations()` — they run on every startup, so they must be safe to re-run.
- Anything that can place a real order must keep the existing safety rails (`max_order_value`, balance verification, fill reconciliation). If in doubt, ask in the issue first.

**Frontend (React 19, Vite, Tailwind CSS 4)**
- Plain JavaScript/JSX — **no TypeScript**.
- `npm run lint` and `npm run build` (in `frontend/`) must both pass.
- Use the primitives in `frontend/src/components/ui/` and the tokens described in [`DESIGN.md`](frontend/src/DESIGN.md): no ad-hoc hex colours, no `alert()`/`confirm()` — use `toast.*` and `ConfirmDialog`.

## Adding an exchange

Any CCXT-compatible exchange can be added: register it in `SUPPORTED_EXCHANGES` in `backend/core/exchange_registry.py` (including its capabilities/setup guide) and add it to the exchange dropdowns in the frontend. Please test candle backfill, balance fetching and — if the exchange has a sandbox — paper execution before opening the PR, and mention which of those you verified.

## Contributing strategy examples

Pull requests adding strategies to [`examples/`](examples/) are very welcome. Requirements:

- Export from the builder as `.apex.json` and name it `Descriptive_Name.apex.json`.
- No API key names, `api_execution` must be `false`, sandbox flags off.
- Include a short description of the idea and the backtest period/pair(s) you validated it on in the PR. Educational value counts more than headline returns.
- Paste the output of `python scripts/verify_examples.py --only <YourFile>` in the PR — that is the number we document for it in `STRATEGY_CONTEXT.md` §4.9.
- Run `python -m pytest -q tests/test_golden_backtest.py` once: it writes `tests/golden/<YourFile>.json` for the new example. Commit that file too; from then on the engine tests guard your strategy's trades.

## License of contributions

ApexAlgo is licensed under the GNU AGPL-3.0 (see [LICENSE](LICENSE)). By submitting a contribution you agree that it is licensed under the same terms and that you have the right to contribute it.
