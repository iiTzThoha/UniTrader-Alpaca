"""
Options chain + implied volatility data, sourced from Alpaca's Market Data
API (options snapshots include Greeks + IV per contract).

Important nuance on IV Rank / IV Percentile:
Alpaca gives us a POINT-IN-TIME implied volatility per contract (today's
IV), not a ready-made "IV Rank" or "IV Percentile" - those are inherently
historical metrics (today's IV relative to its own trailing range, e.g.
252 trading days). There is no shortcut around this: we must accumulate
our OWN daily history of each underlying's IV (via the `signals` table
we already built) and compute rank/percentile from that accumulated
history over time.

This means IV Rank/Percentile will be *unavailable* (None) for the first
day or two of the hackathon until enough daily snapshots exist, and will
become more statistically meaningful as more days accumulate. This is
disclosed clearly rather than faked with synthetic history.

HV-IV spread, by contrast, doesn't need long history - it's today's IV
minus the underlying's own recent historical (realized) volatility, and
we already compute HV inputs from the technicals module's price history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from config.settings import settings
from core.alpaca_client import get_trading_client


@dataclass
class OptionsSnapshot:
    symbol: str                      # underlying symbol
    as_of: datetime
    atm_iv: float | None             # at-the-money implied volatility (today, point-in-time)
    iv_rank: float | None            # 0-100, None until enough history accumulated
    iv_percentile: float | None      # 0-100, None until enough history accumulated
    hv_iv_spread: float | None       # atm_iv - historical_volatility (both as decimals, e.g. 0.25)
    nearest_expiry: date | None
    contracts_found: int


def _get_options_data_client() -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
    )


def _find_nearest_expiry_contracts(symbol: str, min_dte: int = 7, max_dte: int = 45) -> list[str]:
    """Finds option contract symbols for `symbol` expiring within
    [min_dte, max_dte] days from today, to use as our IV read point.
    We use a near-the-money, near-term contract as the proxy for
    "the underlying's current implied volatility" - a common simplification
    (true term-structure/skew modeling is out of scope for this build)."""
    trading_client = get_trading_client()

    today = date.today()
    request = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status="active",
        expiration_date_gte=today + timedelta(days=min_dte),
        expiration_date_lte=today + timedelta(days=max_dte),
        type=ContractType.CALL,  # calls are enough for an ATM IV proxy read
        limit=50,
    )
    response = trading_client.get_option_contracts(request)
    contracts = response.option_contracts if hasattr(response, "option_contracts") else response

    return [c.symbol for c in contracts]


def fetch_atm_iv(symbol: str, underlying_price: float) -> tuple[float | None, date | None, int]:
    """Fetches the options chain and returns the IV of the contract with
    strike closest to the current underlying price THAT ACTUALLY HAS a
    valid IV reading. Alpaca cannot compute IV/Greeks for contracts with
    a zero bid (illiquid/worthless) - see docs.alpaca.markets market-data-faq.
    So we sort ALL contracts by strike distance and walk outward until we
    find one with usable data, rather than assuming the single closest
    strike will have it. Returns (atm_iv, nearest_expiry, contracts_found)."""
    data_client = _get_options_data_client()

    request = OptionChainRequest(underlying_symbol=symbol)
    chain = data_client.get_option_chain(request)

    if not chain:
        return None, None, 0

    # Build a list of (strike_distance, iv, expiry) for every contract
    # that has BOTH a strike price and a valid (non-None) implied_volatility.
    candidates: list[tuple[float, float, date | None]] = []

    for contract_symbol, snapshot in chain.items():
        iv = getattr(snapshot, "implied_volatility", None)
        if iv is None:
            continue  # Alpaca couldn't compute IV for this contract (e.g. zero bid)

        # Strike price isn't always a direct attribute on the snapshot -
        # parse it from the OCC-style symbol suffix as a reliable fallback.
        strike = _parse_strike_from_occ_symbol(contract_symbol)
        if strike is None:
            continue

        expiry = _parse_expiry_from_occ_symbol(contract_symbol)
        distance = abs(strike - underlying_price)
        candidates.append((distance, float(iv), expiry))

    if not candidates:
        # Found contracts, but none had a computable IV (all illiquid/zero-bid)
        return None, None, len(chain)

    candidates.sort(key=lambda c: c[0])  # closest strike to underlying first
    _, best_iv, best_expiry = candidates[0]

    return best_iv, best_expiry, len(chain)


def _parse_strike_from_occ_symbol(occ_symbol: str) -> float | None:
    """OCC option symbols encode the strike as the last 8 digits, in
    thousandths of a dollar. E.g. 'AAPL260918P00075000' -> strike 75.000.
    Format: {root}{YYMMDD}{C/P}{strike*1000, zero-padded to 8 digits}"""
    try:
        strike_str = occ_symbol[-8:]
        return int(strike_str) / 1000.0
    except (ValueError, IndexError):
        return None


def _parse_expiry_from_occ_symbol(occ_symbol: str) -> date | None:
    """OCC symbols encode expiry as YYMMDD right before the C/P flag.
    E.g. 'AAPL260918P00075000' -> 2026-09-18."""
    try:
        # Find the C/P character - it's the 9th-from-last character
        cp_index = len(occ_symbol) - 9
        date_str = occ_symbol[cp_index - 6:cp_index]  # YYMMDD
        return datetime.strptime(date_str, "%y%m%d").date()
    except (ValueError, IndexError):
        return None


def compute_historical_volatility(close_prices: pd.Series, window: int = 20) -> float | None:
    """Annualized historical (realized) volatility from daily log returns,
    used as the other half of the HV-IV spread indicator."""
    if len(close_prices) < window + 1:
        return None
    log_returns = np.log(close_prices / close_prices.shift(1)).dropna()
    daily_std = log_returns.tail(window).std()
    annualized = daily_std * np.sqrt(252)
    return float(annualized)


def compute_iv_rank_percentile(
    current_iv: float,
    historical_ivs: list[float],
) -> tuple[float | None, float | None]:
    """Given today's IV and a list of past daily IV readings (accumulated
    from our own `signals` table over time, ideally ~252 trading days),
    computes IV Rank and IV Percentile.

    IV Rank = (current_iv - min) / (max - min) * 100
    IV Percentile = % of historical readings below current_iv

    Returns (None, None) if fewer than 10 historical readings exist -
    below that, rank/percentile are too noisy to be meaningful.
    """
    if len(historical_ivs) < 10:
        return None, None

    all_ivs = historical_ivs + [current_iv]
    iv_min, iv_max = min(all_ivs), max(all_ivs)

    if iv_max == iv_min:
        iv_rank = 50.0  # no variance to rank against
    else:
        iv_rank = (current_iv - iv_min) / (iv_max - iv_min) * 100

    below_count = sum(1 for iv in historical_ivs if iv < current_iv)
    iv_percentile = (below_count / len(historical_ivs)) * 100

    return round(iv_rank, 2), round(iv_percentile, 2)


def compute_options_snapshot(
    symbol: str,
    underlying_price: float,
    close_prices: pd.Series,
    historical_ivs: list[float] | None = None,
) -> OptionsSnapshot:
    """Main entrypoint: combines ATM IV lookup, HV computation, and
    IV rank/percentile (if history is available) into one snapshot.

    `historical_ivs` should be pulled from the `signals` table by the
    caller (Day 3+ orchestration) - this function stays pure/testable
    and doesn't reach into Supabase itself.
    """
    atm_iv, nearest_expiry, contracts_found = fetch_atm_iv(symbol, underlying_price)

    hv = compute_historical_volatility(close_prices)
    hv_iv_spread = (atm_iv - hv) if (atm_iv is not None and hv is not None) else None

    iv_rank, iv_percentile = (None, None)
    if atm_iv is not None and historical_ivs:
        iv_rank, iv_percentile = compute_iv_rank_percentile(atm_iv, historical_ivs)

    return OptionsSnapshot(
        symbol=symbol,
        as_of=datetime.now(timezone.utc),
        atm_iv=atm_iv,
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
        hv_iv_spread=hv_iv_spread,
        nearest_expiry=nearest_expiry,
        contracts_found=contracts_found,
    )


if __name__ == "__main__":
    # Smoke test: `python -m signals.options_data`
    # Requires real Alpaca credentials in .env
    from signals.technicals import fetch_price_history

    test_symbol = "AAPL"
    df = fetch_price_history(test_symbol)
    price = float(df["Close"].iloc[-1])

    snapshot = compute_options_snapshot(test_symbol, price, df["Close"])
    print(snapshot)
