"""
Thin Alpaca client wrapper using alpaca-py (Trading API).

All clients constructed here are explicitly pinned to paper=True as a
safety guardrail on top of the paper-URL check in config/settings.py's
validate(). Two independent checks against ever touching a live account
by accident.
"""

from __future__ import annotations

from functools import lru_cache

from alpaca.trading.client import TradingClient

from config.settings import settings


@lru_cache(maxsize=1)
def get_trading_client() -> TradingClient:
    """Returns a cached Alpaca TradingClient, hardcoded to paper trading."""
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise RuntimeError(
            "Alpaca credentials missing. Check your .env file has "
            "ALPACA_API_KEY and ALPACA_SECRET_KEY set."
        )
    return TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=True,  # hardcoded - this project must never trade live
    )


if __name__ == "__main__":
    # Smoke test: `python -m core.alpaca_client`
    # Requires a real .env with valid Alpaca paper credentials.
    client = get_trading_client()
    account = client.get_account()
    print("Connected to Alpaca paper trading successfully.")
    print(f"Account status: {account.status}")
    print(f"Equity: ${account.equity}")
    print(f"Buying power: ${account.buying_power}")
    print(f"Pattern day trader: {account.pattern_day_trader}")
