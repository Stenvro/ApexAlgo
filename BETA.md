# ApexAlgo — Beta Tester Guide

> Applies to **v2.0.0** and later (v2 changed how forward tests and `max_positions` behave — see the release notes if you upgrade from v1).

Welcome to the ApexAlgo beta! ApexAlgo is a self-hosted, no-code crypto trading platform: you build strategies visually (or import them), backtest them on real historical data, and run them in paper or live mode against your own exchange account.

**Beta status:** this software is under active development. Expect rough edges, report everything that surprises you, and never trade with money you cannot afford to lose.

---

## 1. Requirements

- **Docker** with the Compose plugin (`docker compose version` should work). Linux or macOS.
- Free ports **5173** (web UI) and **8000** (API).
- ~2 GB of free disk space (historical candle data grows over time).

## 2. Install & first start

```bash
git clone https://github.com/Stenvro/ApexAlgo.git
cd ApexAlgo
docker compose up -d
```

The first start takes a few minutes: the backend generates its configuration and TLS certificates, and the frontend waits for the backend to become healthy before building the web bundle. **Don't interrupt it.** Follow along with:

```bash
docker compose logs -f
```

You're ready when the frontend logs `Starting nginx`.

## 3. Log in

1. Open **https://localhost:5173** in your browser.
2. Your browser will warn about a self-signed certificate — this is expected for a local app. Click *Advanced → Proceed / Accept the risk*. (The certificate is generated on your own machine; nobody else has it.)
3. The login screen asks for your **API key**. It was generated on first start and lives on your machine in `data/.env`:

   ```bash
   grep MASTER_API_KEY data/.env
   ```

   If permission is denied: `sudo grep MASTER_API_KEY data/.env`.
4. Paste the key and sign in. You enter it once; the backend sets a session cookie (the key itself is never stored in the browser). After a backend restart (`docker compose restart backend`, update, reboot) you log in again.

> **Troubleshooting login:** if the app says it cannot reach the backend, wait a minute (first start is slow) and try again. Check `docker compose ps` — both containers should be `Up`, backend `healthy`.

## 4. Your first bot (5 minutes, no exchange account needed)

1. Go to **Algorithms** and click **Load example strategy** (or *Import* and pick a file from the `examples/` folder in the repo — `Supertrend_Trend_1d.apex.json` is a good start; `Supertrend_LongShort_Perp_1d.apex.json` is the long/short perpetual version).
2. Leave the bot in **Paper** mode with *Run Historical Backtest* enabled.
3. Click **Start**. The engine downloads historical candles (a few minutes the first time) and runs a full backtest. Open the **Console** on the bot card to watch it work.
4. Explore the results in **Trades** (equity curve, drawdown, positions) and on the **Chart** (buy/sell markers).

That's the core loop. Build your own strategies in the **Builder**: Indicator → Condition → (Logic Gate) → Entry/Exit Action, plus stop-losses and take-profits.

### Let an AI design a strategy for you

The repo contains `STRATEGY_CONTEXT.md`. Paste that file into any capable AI assistant (Claude, ChatGPT, …) together with a request like *"design a conservative BTC swing strategy"* and it will produce a ready-to-import `.apex.json` file. Save the JSON, import it via **Algorithms → Import**, and backtest it. The import is validated — invalid files are rejected with a clear error list.

## 5. Going live with a real exchange account

**Do this only after a strategy has run in paper/forward-test mode for at least a week and you understand its behavior.** Paper fills are simulated; live results will differ (fees, slippage, partial fills).

### Protect yourself first (required reading)

1. Create a **dedicated API key just for ApexAlgo** on your exchange — never reuse keys from other tools.
2. Give the key **trade + read permissions only. Withdrawals/transfers must be OFF.** This is the single most important rule: even in the worst case, money cannot leave your account.
3. Set an **IP allowlist** on the key (your home IP) if your exchange supports it.
4. Use a **sub-account with a small test balance** (think €50–€200) — keep your main funds out of the key's reach. The bot sizes trades from the account's free balance (capped by the bot's capital setting), so a dedicated small account is your best protection.
5. **One live key per bot.** Don't share a key between bots and don't trade manually on the bot's account — the bot cannot see what it didn't do itself.

### Configure the bot for live

- Add your exchange key under **Settings** (it is encrypted at rest).
- In the bot: enable API execution and link the key.
- **`Max order value` is mandatory for live bots** — the app refuses to start a live bot without this hard cap per order. Set it low.
- Set **Max drawdown** (e.g. 10–15%): by default the bot then automatically closes its positions and stops if the equity curve drops that far from its peak. You can switch **On max drawdown** to *Block new entries* instead (exits keep working, no forced liquidation) — if you do, also set **Max capital loss %** as the hard stop, since blocking entries alone does not cap losses on open positions.
- Fill in your exchange's real **fee** (e.g. 0.1%) in the trade settings — backtests without fees are misleadingly optimistic.
- After any backend restart the bot reconciles its open positions with your exchange balances (or, on perpetual swaps, the exchange's position list) before going live and stops with an error if they don't match or cannot be checked — still glance at the exchange yourself before letting it continue.
- **`Max order value` is a cap on the quote notional** (e.g. USDT for `BTC/USDT`), not on the margin you put up — at 5× leverage a 500 cap lets the bot risk a 100 margin. It applies in backtest and forward test too, so changing it counts as a new strategy variant.

### Perpetual swaps: known limitations of the simulation

Backtest and forward test model leverage, margin, fees, slippage and liquidation (flat 0.5% maintenance margin), but **not**:

- **Funding payments** — perps pay/receive funding every few hours; a long-running position can lose (or gain) a few percent that the simulation does not show.
- **Tiered maintenance margin** — big positions liquidate earlier on the real exchange than the flat 0.5% suggests.
- **Cross margin** — `cross` is sent to the exchange, but the simulation treats it like isolated (one position cannot drain the whole account in the backtest; on the exchange it can).
- **Spot margin / borrowing** — not supported; spot bots only ever spend the cash they hold.
- **Hedge mode** — not supported; keep the account in one-way position mode. The bot never holds a long and a short on the same pair at once, and reconciliation nets both sides into one number, so hedged positions would be misread.

Whatever you do on the exchange yourself (manual trades, changing the leverage, switching position mode) is invisible to the bot until the next restart.

### If you suspect a leak or anything weird

1. **Disable the API key on the exchange immediately** (this beats everything else).
2. `docker compose down` to stop all bots.
3. Check your exchange's order and login history.
4. Report the incident (see §8) with timestamps — but never share your `data/` folder, `.env` file, or database; they contain your keys.

## 6. LAN access (optional)

By default ApexAlgo only listens on `127.0.0.1` (your own machine). To reach the UI from other devices on your network:

```bash
BIND_ADDR=0.0.0.0 docker compose up -d
```

Then browse to `https://<your-LAN-IP>:5173` and accept the certificate warning there too. Never expose these ports to the internet (no router port-forwarding, no public reverse proxy) — the master key is all that stands between the internet and your exchange account.

## 7. Maintenance

- **Update:** `git pull && docker compose build && docker compose up -d`
- **Logs:** `docker compose logs -f backend` (engine) / `frontend` (web)
- **Restart:** `docker compose restart backend`
- **Full reset (destroys all data, bots and keys):** `docker compose down`, delete the `data/` folder, start again. You'll get a new master key and must re-enter exchange keys.
- Don't delete candle data while bots are actively backfilling — stop the bots first.

## 8. Reporting feedback

Please report bugs and confusion via [GitHub Issues](https://github.com/Stenvro/ApexAlgo/issues). Include:

- What you did, what you expected, what happened.
- Output of `docker compose logs backend | tail -50` if relevant.
- Your OS and browser.
- **Never** attach `data/.env`, the database, or screenshots showing your master key.

Every "this confused me" report is as valuable as a bug report. Thank you for testing!
