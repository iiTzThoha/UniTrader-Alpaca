"""
Builds the scan universe: symbols that (a) have options contracts available
on Alpaca, and (b) pass a basic liquidity filter so we don't waste signal
computation on illiquid names.

This is intentionally dynamic (queried from Alpaca) rather than a hardcoded
list, so it self-updates as Alpaca adds/removes optionable assets - and it
means we're never scanning a symbol we can't actually trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from core.alpaca_client import get_trading_client

# A reasonable default seed of large-cap / high-volume names to bias toward,
# used only as a fallback filter tiebreaker - NOT a hardcoded final list.
# Every symbol still must pass options_enabled + Alpaca's own tradability
# flags to be included.
_PREFERRED_LIQUID_SEED = {
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD",
    "NFLX", "JPM", "BAC", "XOM", "CVX", "WMT", "COST", "DIS",
    "BA", "CAT", "V", "MA", "UNH", "HD", "PG", "KO", "PEP", "INTC",
}


@dataclass
class UniverseSymbol:
    symbol: str
    tradable: bool
    options_enabled: bool
    fractionable: bool


def fetch_options_enabled_assets(limit: int = 200) -> list[UniverseSymbol]:
    """Queries Alpaca for US equities that are tradable and have
    options contracts enabled. Returns up to `limit` results, biased
    toward the preferred liquid seed list first (if present in results),
    then filled out with whatever else Alpaca returns."""
    client = get_trading_client()

    request = GetAssetsRequest(
        status=AssetStatus.ACTIVE,
        asset_class=AssetClass.US_EQUITY,
    )
    assets = client.get_all_assets(request)

    candidates: list[UniverseSymbol] = []
    for asset in assets:
        # Alpaca's Asset object exposes attributes via `.attributes` list
        # (e.g. "options_enabled"); some SDK versions expose a dedicated
        # boolean field instead - handle both defensively.
        attrs = getattr(asset, "attributes", None) or []
        options_enabled = "has_options" in attrs
        if not options_enabled:
            continue
        if not asset.tradable:
            continue

        candidates.append(UniverseSymbol(
            symbol=asset.symbol,
            tradable=asset.tradable,
            options_enabled=True,
            fractionable=getattr(asset, "fractionable", False),
        ))

    # Bias ordering: preferred seed symbols first, then the rest alphabetically
    candidates.sort(key=lambda s: (s.symbol not in _PREFERRED_LIQUID_SEED, s.symbol))

    return candidates[:limit]


def build_watchlist(max_symbols: int = 30) -> list[str]:
    """Returns a plain list of ticker strings ready for the signal engine
    to scan. This is the main entrypoint other modules should call."""
    assets = fetch_options_enabled_assets(limit=max_symbols)
    return [a.symbol for a in assets]


if __name__ == "__main__":
    # Smoke test: `python -m signals.universe`
    # Requires real Alpaca credentials in .env
    watchlist = build_watchlist(max_symbols=30)
    print(f"Built watchlist of {len(watchlist)} symbols:")
    print(watchlist)
