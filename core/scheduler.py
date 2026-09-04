"""
Scheduler: runs the full signal -> propose -> critique -> (execute)
pipeline across the entire watchlist in one pass.

Gated by check_pre_scan() BEFORE touching any symbol - if the circuit
breaker is tripped, the entire scan is skipped, no signals generated,
no proposals created, nothing that could lead toward a trade. This is
the primary place check_pre_scan() actually matters in practice.

This module does not itself handle "when to run" (cron, while-loop with
sleep, etc.) - it exposes run_scan_cycle() as a single pass, so it can
be invoked by whatever scheduling mechanism is chosen (a simple loop
for the hackathon demo, cron, GitHub Actions, etc.) without coupling
the scan logic to a specific scheduling implementation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agents.orchestrator import run_pipeline
from core.circuit_breaker import check_pre_scan
from signals.universe import build_watchlist


@dataclass
class ScanCycleResult:
    breaker_tripped: bool
    symbols_scanned: int
    proposals_generated: int
    executions_attempted: int
    skipped_reason: str | None = None


def run_scan_cycle(watchlist: list[str] | None = None, delay_between_symbols: float = 1.0) -> ScanCycleResult:
    """Runs one full scan cycle across the watchlist. If not provided,
    pulls a fresh watchlist from signals.universe.build_watchlist().

    delay_between_symbols adds a small pause between symbols to avoid
    hammering yfinance/Alpaca rate limits during a full-watchlist run -
    tune down if scans are too slow, tune up if you hit rate limit errors.
    """
    breaker_result = check_pre_scan()
    if breaker_result.tripped:
        reasons = [r.value for r in breaker_result.reasons]
        print(f"Circuit breaker TRIPPED ({reasons}) - skipping entire scan cycle.")
        return ScanCycleResult(
            breaker_tripped=True, symbols_scanned=0, proposals_generated=0,
            executions_attempted=0, skipped_reason=str(reasons),
        )

    symbols = watchlist or build_watchlist()
    print(f"Circuit breaker OK - scanning {len(symbols)} symbols.")

    proposals_generated = 0
    executions_attempted = 0

    for i, symbol in enumerate(symbols):
        try:
            result = run_pipeline(symbol)
            if result is not None:
                proposals_generated += 1
                if result.get("status") == "executed":
                    executions_attempted += 1
        except Exception as e:
            # One symbol failing (e.g. no options chain, yfinance hiccup)
            # should not abort the whole scan cycle - log and continue.
            print(f"[{symbol}] ERROR during pipeline run: {e}")

        if i < len(symbols) - 1:
            time.sleep(delay_between_symbols)

    print(f"\nScan cycle complete: {len(symbols)} symbols scanned, "
          f"{proposals_generated} proposals generated, "
          f"{executions_attempted} executions attempted.")

    return ScanCycleResult(
        breaker_tripped=False,
        symbols_scanned=len(symbols),
        proposals_generated=proposals_generated,
        executions_attempted=executions_attempted,
    )


if __name__ == "__main__":
    # Smoke test: `python -m core.scheduler`
    # WARNING: in AUTO execution mode, this can place REAL paper orders
    # for every approved proposal across the whole watchlist. Consider
    # testing with a small explicit watchlist first, e.g.:
    #   run_scan_cycle(watchlist=["AAPL", "MSFT"])
    result = run_scan_cycle(watchlist=["AAPL", "MSFT"])
    print(f"\n{result}")
