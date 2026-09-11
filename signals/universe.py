"""
Builds the scan universe: 100 pre-ranked symbols that are large-cap,
optionable on Alpaca, and have meaningful options volume.

This list is curated (not queried for market cap, which Alpaca doesn't
provide) and should be refreshed quarterly from S&P 100 rebalances and
most-active options reports.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from core.alpaca_client import get_trading_client

# 100 pre-ranked symbols. Order = scan priority.
_RANKED_UNIVERSE = [
    # Tier 1: Top options volume + mega-cap (ranks 1-20)
    "NVDA", "TSLA", "AAPL", "MSFT", "AMZN",
    "META", "GOOGL", "GOOG", "AVGO", "PLTR",
    "AMD", "NFLX", "MSTR", "JPM", "BAC",
    "WFC", "GS", "MS", "UNH", "JNJ",

    # Tier 2: Large-cap, high liquidity (ranks 21-40)
    "LLY", "ABBV", "MRK", "WMT", "COST",
    "HD", "MCD", "PG", "KO", "PEP",
    "XOM", "CVX", "CAT", "BA", "DIS",
    "NKE", "V", "MA", "ORCL", "CSCO",

    # Tier 3: Active options + index ETFs (ranks 41-60)
    "IBM", "QCOM", "TXN", "MU", "AMAT",
    "LRCX", "KLAC", "PANW", "CRWD", "ANET",
    "GE", "RTX", "HON", "UPS", "LMT",
    "DELL", "SNDK", "SPY", "QQQ", "IWM",

    # Tier 4: Quality fillers, optionable, not alphabet junk (ranks 61-80)
    "DIA", "TSM", "AMGN", "GILD", "PFE",
    "TMO", "ABT", "DHR", "LIN", "HON",
    "NEE", "DUK", "SO", "AEP", "USB",
    "PNC", "TFC", "SCHW", "BLK", "AXP",

    # Tier 5: Remaining S&P 100 + high-options names (ranks 81-100)
    "INTU", "ISRG", "NOW", "UBER", "BKNG",
    "SBUX", "TGT", "LOW", "MDLZ", "PM",
    "MO", "CVS", "MDT", "BMY", "GILD",
    "F", "GM", "AIG", "MET", "SPG",
]


@dataclass
class UniverseSymbol:
    symbol: str
    tradable: bool
    options_enabled: bool
    fractionable: bool


def fetch_options_enabled_assets(limit: int = 100) -> list[UniverseSymbol]:
    """Returns up to `limit` symbols from the ranked universe, filtered
    to those Alpaca confirms are tradable and optionable."""
    client = get_trading_client()

    request = GetAssetsRequest(
        status=AssetStatus.ACTIVE,
        asset_class=AssetClass.US_EQUITY,
    )
    assets = client.get_all_assets(request)

    optionable: set[str] = set()
    for asset in assets:
        attrs = getattr(asset, "attributes", None) or []
        if "has_options" not in attrs:
            continue
        if not asset.tradable:
            continue
        optionable.add(asset.symbol)

    # Walk the ranked list, keep only Alpaca-confirmed optionable names.
    selected = [s for s in _RANKED_UNIVERSE if s in optionable]

    # If Alpaca dropped something, backfill from the ranked list isn't
    # possible without extras -- so just return what we have.
    return [
        UniverseSymbol(
            symbol=s,
            tradable=True,
            options_enabled=True,
            fractionable=False,
        )
        for s in selected[:limit]
    ]


def build_watchlist(max_symbols: int = 100) -> list[str]:
    """Returns a plain list of ticker strings ready for the signal engine
    to scan. This is the main entrypoint other modules should call."""
    assets = fetch_options_enabled_assets(limit=max_symbols)
    return [a.symbol for a in assets]


if __name__ == "__main__":
    watchlist = build_watchlist(max_symbols=100)
    print(f"Built watchlist of {len(watchlist)} symbols:")
    print(watchlist)
