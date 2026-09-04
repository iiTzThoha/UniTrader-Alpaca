"""
Technical indicator computation on underlying equity price history.

Uses yfinance for OHLCV data (free, no extra API key, good historical
depth) - deliberately NOT Alpaca's free-tier stock data, which has a
15-minute delay and shallower history on the free plan. Alpaca is instead
reserved for what it's uniquely good for: options chain + IV data (see
signals/options_data.py).

All functions here are pure (take a DataFrame, return a value/Series) so
they're independently unit-testable without hitting any network.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class TechnicalSnapshot:
    symbol: str
    underlying_price: float
    rsi: float | None
    ma_trend: str | None          # 'bullish' | 'bearish' | 'neutral'
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    atr: float | None
    volume: int | None
    avg_volume_20d: float | None


def fetch_price_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Fetches OHLCV history for a symbol via yfinance.
    6mo/1d gives enough bars for a 200-day-adjacent MA trend read while
    staying fast; adjust period if longer MAs are needed later."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval)
    if df.empty:
        raise ValueError(f"No price history returned for {symbol}")
    return df


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Standard Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr


def compute_ma_trend(close: pd.Series, short_window: int = 20, long_window: int = 50) -> str:
    """Simple trend read: short MA vs long MA crossover state.
    Requires at least `long_window` bars of history to be meaningful -
    returns 'neutral' if insufficient data rather than raising, since
    this is a soft signal not a hard requirement."""
    if len(close) < long_window:
        return "neutral"

    short_ma = close.rolling(short_window).mean().iloc[-1]
    long_ma = close.rolling(long_window).mean().iloc[-1]

    if pd.isna(short_ma) or pd.isna(long_ma):
        return "neutral"

    # small buffer to avoid flip-flopping right at the crossover point
    spread_pct = (short_ma - long_ma) / long_ma * 100
    if spread_pct > 0.5:
        return "bullish"
    elif spread_pct < -0.5:
        return "bearish"
    return "neutral"


def compute_technical_snapshot(symbol: str) -> TechnicalSnapshot:
    """Main entrypoint: fetches history and computes all technical
    indicators for one symbol in one pass."""
    df = fetch_price_history(symbol)
    close = df["Close"]

    rsi_series = compute_rsi(close)
    macd_line, signal_line, histogram = compute_macd(close)
    atr_series = compute_atr(df)
    trend = compute_ma_trend(close)

    latest_volume = int(df["Volume"].iloc[-1]) if not df["Volume"].empty else None
    avg_volume_20d = float(df["Volume"].tail(20).mean()) if len(df) >= 1 else None

    def _last_or_none(series: pd.Series) -> float | None:
        val = series.iloc[-1] if len(series) else None
        return None if val is None or pd.isna(val) else float(val)

    return TechnicalSnapshot(
        symbol=symbol,
        underlying_price=float(close.iloc[-1]),
        rsi=_last_or_none(rsi_series),
        ma_trend=trend,
        macd=_last_or_none(macd_line),
        macd_signal=_last_or_none(signal_line),
        macd_histogram=_last_or_none(histogram),
        atr=_last_or_none(atr_series),
        volume=latest_volume,
        avg_volume_20d=avg_volume_20d,
    )


if __name__ == "__main__":
    # Smoke test: `python -m signals.technicals`
    # No Alpaca/Supabase credentials needed - yfinance is public.
    snapshot = compute_technical_snapshot("AAPL")
    print(snapshot)
