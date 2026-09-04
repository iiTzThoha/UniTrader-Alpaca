"""
Combined signal engine: orchestrates technicals (yfinance) + options/IV
data (Alpaca) into one full signal row per symbol, matching the `signals`
table schema, and persists it to Supabase.

This is the Day 2 capstone - running this module's `scan_and_store()`
for a symbol produces exactly one row in the `signals` table with every
indicator populated (or explicitly None where not yet computable, e.g.
IV rank on day 1 of accumulation).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import yfinance as yf

from config.settings import Timeframe, settings
from persistence.supabase_client import get_client
from signals.options_data import compute_options_snapshot
from signals.technicals import compute_technical_snapshot, fetch_price_history


@dataclass
class FullSignal:
    symbol: str
    timeframe: str
    created_at: str
    underlying_price: float

    # Technicals
    rsi: float | None
    ma_trend: str | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    atr: float | None
    volume: int | None
    avg_volume_20d: int | None
    market_cap: float | None

    # Options/IV
    iv_rank: float | None
    iv_percentile: float | None
    hv_iv_spread: float | None

    raw_data: dict


def fetch_market_cap(symbol: str) -> float | None:
    """Pulled separately from yfinance's `fast_info` (lighter weight than
    the full `.info` call, which is slow and sometimes rate-limited)."""
    try:
        ticker = yf.Ticker(symbol)
        cap = ticker.fast_info.get("marketCap") if hasattr(ticker, "fast_info") else None
        return float(cap) if cap else None
    except Exception:
        # Market cap is a nice-to-have, not critical - never let it break the scan
        return None


def fetch_historical_ivs(symbol: str, lookback_days: int = 252) -> list[float]:
    """Pulls prior daily IV readings for `symbol` from our own `signals`
    table, so IV Rank/Percentile can be computed against real accumulated
    history rather than synthetic data. Returns an empty list on the
    first day(s) of the hackathon before enough history exists - this is
    expected and handled gracefully upstream (see options_data.py)."""
    client = get_client()
    result = (
        client.table("signals")
        .select("raw_data, created_at")
        .eq("symbol", symbol)
        .order("created_at", desc=True)
        .limit(lookback_days)
        .execute()
    )
    ivs = []
    for row in result.data:
        raw = row.get("raw_data") or {}
        iv = raw.get("atm_iv")
        if iv is not None:
            ivs.append(float(iv))
    return ivs


def build_signal(symbol: str, timeframe: Timeframe = Timeframe.MEDIUM) -> FullSignal:
    """Computes one full signal snapshot for a symbol. Does NOT persist -
    call store_signal() separately so this stays testable in isolation."""
    price_df = fetch_price_history(symbol)
    tech = compute_technical_snapshot(symbol)
    market_cap = fetch_market_cap(symbol)

    historical_ivs = fetch_historical_ivs(symbol)
    options = compute_options_snapshot(
        symbol=symbol,
        underlying_price=tech.underlying_price,
        close_prices=price_df["Close"],
        historical_ivs=historical_ivs,
    )

    return FullSignal(
        symbol=symbol,
        timeframe=timeframe.value,
        created_at=datetime.now(timezone.utc).isoformat(),
        underlying_price=tech.underlying_price,
        rsi=tech.rsi,
        ma_trend=tech.ma_trend,
        macd=tech.macd,
        macd_signal=tech.macd_signal,
        macd_histogram=tech.macd_histogram,
        atr=tech.atr,
        volume=tech.volume,
        avg_volume_20d=int(tech.avg_volume_20d) if tech.avg_volume_20d else None,
        market_cap=market_cap,
        iv_rank=options.iv_rank,
        iv_percentile=options.iv_percentile,
        hv_iv_spread=options.hv_iv_spread,
        raw_data={
            "atm_iv": options.atm_iv,
            "nearest_expiry": options.nearest_expiry.isoformat() if options.nearest_expiry else None,
            "contracts_found": options.contracts_found,
        },
    )


def store_signal(signal: FullSignal) -> dict:
    """Persists a FullSignal to the `signals` table in Supabase."""
    client = get_client()
    row = asdict(signal)
    result = client.table("signals").insert(row).execute()
    return result.data[0] if result.data else {}


def scan_and_store(symbol: str, timeframe: Timeframe = Timeframe.MEDIUM) -> FullSignal:
    """Convenience wrapper: build + store in one call."""
    signal = build_signal(symbol, timeframe)
    store_signal(signal)
    return signal


if __name__ == "__main__":
    # Smoke test: `python -m signals.engine`
    # Requires real Alpaca + Supabase credentials in .env
    result = scan_and_store("AAPL")
    print(f"Signal stored for {result.symbol}:")
    for k, v in asdict(result).items():
        print(f"  {k}: {v}")
