# Alpaca AI Trading Agents Hackathon

Paper-trading options bot with a swappable mode architecture (options/equity),
Proposer/Critic multi-LLM risk governance, and a human-in-the-loop or fully
autonomous execution mode, gated by an account-level circuit breaker.

## Project Structure

```
core/               Shared infra: Alpaca client, circuit breaker, orchestration
modes/
  options_mode/     Options-specific strategy logic (primary mode for this hackathon)
  equity_mode/      Equity-specific strategy logic (secondary/future mode)
signals/            Indicator computation (IV Rank, RSI, MACD, ATR, etc.)
agents/             Proposer and Critic LLM agents
execution/          Order submission, manual review queue, wishlist logic
persistence/        Supabase client + SQL migrations
dashboard/          Demo dashboard
config/             Central settings, thresholds, timeframe rules
scripts/            One-off utility scripts (migrations, smoke tests)
tests/              Unit tests
```

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Fill in `.env` with your real Alpaca paper API keys and Supabase credentials.
   Never commit `.env` - it's already in `.gitignore`.

3. **Apply the database schema**
   Open your Supabase project's SQL Editor and run the contents of
   `persistence/migrations/001_initial_schema.sql`.
   (Run `python -m scripts.run_migration` for a reminder of the file path.)

4. **Verify config is valid**
   ```bash
   python -m config.settings
   ```
   Should print "Configuration OK." If not, it'll list exactly what's missing.

5. **Smoke test Alpaca connection**
   ```bash
   python -m core.alpaca_client
   ```
   Should print your paper account's equity and buying power.

6. **Smoke test Supabase connection**
   ```bash
   python -m persistence.supabase_client
   ```
   Should print a successful connection message.

7. **Smoke test the circuit breaker logic (no credentials needed)**
   ```bash
   python -m core.circuit_breaker
   ```

## Safety guardrails in place

- `TradingClient` is hardcoded with `paper=True` in `core/alpaca_client.py`
- `Settings.validate()` hard-fails if `ALPACA_BASE_URL` doesn't contain `paper-api`
- Circuit breaker checked pre-scan and pre-execution (once wired up), sticky pause
  requires human clearing - never auto-resumes on a timer
- `.env` is gitignored; `.env.example` has placeholders only

## Status

**Day 1: Foundations** - scaffold, config, circuit breaker skeleton, DB schema. ✅

Next: Day 2 - signal engine (indicators via yfinance) + Alpaca options chain integration.
