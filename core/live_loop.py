"""
Live loop: runs the full autonomous cycle unattended.

Each cycle:
  1. run_scan_cycle()  - signal -> propose -> critique -> (execute) across
     the watchlist. Gated internally by check_pre_scan().
  2. run_exit_check()  - evaluates every open position's combined P&L and
     auto-closes anything that has crossed +50%/-50%. Gated internally by
     check_pre_execution().

This is the piece that makes the system actually autonomous end-to-end:
previously both steps only ran when a human clicked a dashboard button.
Run this in its own terminal/process, separate from the dashboard, which
stays purely a viewing/control surface (Review Queue, manual overrides,
circuit breaker controls, live P&L).

Usage:
    python -m core.live_loop
    python -m core.live_loop --interval 300          # 5 min between cycles
    python -m core.live_loop --watchlist AAPL MSFT    # restrict scan universe

Stop with Ctrl+C - the current cycle finishes (it does not hard-kill
mid-order) and the loop exits cleanly.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

from core.scheduler import run_scan_cycle
from execution.close_evaluator import run_exit_check

_STOP_REQUESTED = False


def _handle_sigint(signum, frame):
    global _STOP_REQUESTED
    if _STOP_REQUESTED:
        # Second Ctrl+C: force exit immediately.
        print("\nForce exit.")
        sys.exit(1)
    print("\nStop requested - finishing current cycle, then exiting. "
          "(Press Ctrl+C again to force quit.)")
    _STOP_REQUESTED = True


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _run_one_cycle(watchlist: list[str] | None) -> None:
    print(f"\n{'=' * 70}")
    print(f"[{_ts()}] CYCLE START")
    print(f"{'=' * 70}")

    # --- 1. Scan / propose / critique / execute -----------------------
    print(f"\n[{_ts()}] --- Scan cycle (entries) ---")
    try:
        scan_result = run_scan_cycle(watchlist=watchlist)
        if scan_result.breaker_tripped:
            print(f"[{_ts()}] Scan skipped - circuit breaker tripped "
                  f"({scan_result.skipped_reason}).")
        else:
            print(f"[{_ts()}] Scan complete: "
                  f"{scan_result.symbols_scanned} symbols, "
                  f"{scan_result.proposals_generated} proposals, "
                  f"{scan_result.executions_attempted} executions.")
    except Exception as e:
        # A full-cycle failure (e.g. Supabase/Alpaca outage) should not
        # kill the loop - log it and let the next cycle try again.
        print(f"[{_ts()}] ERROR during scan cycle: {e}")

    # --- 2. Exit check (take-profit / stop-loss) -----------------------
    print(f"\n[{_ts()}] --- Exit check ---")
    try:
        evaluations = run_exit_check()
        if not evaluations:
            print(f"[{_ts()}] No open proposals to evaluate.")
        else:
            for ev in evaluations:
                pct_str = (f"{ev.combined_pct:+.1%}"
                           if ev.combined_pct is not None
                           else "N/A (missing leg data)")
                trigger_str = ev.trigger or "none"
                closed_str = " -> CLOSED" if ev.closed else ""
                print(f"[{_ts()}]   {', '.join(ev.symbols):<20} "
                      f"P&L {pct_str:>8}  trigger={trigger_str}{closed_str}")
                for msg in ev.close_messages:
                    print(f"[{_ts()}]     - {msg}")
    except Exception as e:
        print(f"[{_ts()}] ERROR during exit check: {e}")

    print(f"\n[{_ts()}] CYCLE END")


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous live loop (scan + exit check).")
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Seconds between cycles (default: 300 = 5 minutes).",
    )
    parser.add_argument(
        "--watchlist", nargs="*", default=None,
        help="Explicit symbols to scan (default: fresh watchlist from build_watchlist()).",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)

    print(f"[{_ts()}] Live loop starting. Interval: {args.interval}s. "
          f"Watchlist: {args.watchlist or 'auto (build_watchlist())'}.")
    print(f"[{_ts()}] Press Ctrl+C to stop after the current cycle.\n")

    cycle_num = 0
    while not _STOP_REQUESTED:
        cycle_num += 1
        print(f"\n### Cycle #{cycle_num} ###")
        _run_one_cycle(args.watchlist)

        if _STOP_REQUESTED:
            break

        print(f"\n[{_ts()}] Sleeping {args.interval}s until next cycle...")
        # Sleep in small increments so Ctrl+C is responsive instead of
        # blocking for the full interval.
        slept = 0
        while slept < args.interval and not _STOP_REQUESTED:
            time.sleep(min(1, args.interval - slept))
            slept += 1

    print(f"\n[{_ts()}] Live loop stopped after {cycle_num} cycle(s).")


if __name__ == "__main__":
    main()
