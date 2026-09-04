"""
Account-level circuit breaker.

Design intent (from planning):
- Checked at TWO points: before the autonomous scan loop runs, and again
  immediately before any execution (manual-approved or auto).
- "Sticky pause": once tripped, stays tripped until a human clears it -
  it does NOT auto-resume after cooldown_minutes_after_trip elapses.
  That field is informational/for the dashboard countdown display only.
- Every trip and every check is logged to `circuit_breaker_events` in
  Supabase, so the dashboard can show a full audit trail.

This module is intentionally dependency-light (no Alpaca/Supabase client
construction happens at import time) so it can be unit tested with fake
account snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from config.settings import CircuitBreakerConfig, settings


class TripReason(str, Enum):
    DAILY_LOSS_PCT = "daily_loss_pct_exceeded"
    DAILY_LOSS_ABSOLUTE = "daily_loss_absolute_exceeded"
    MAX_OPEN_POSITIONS = "max_open_positions_exceeded"
    MAX_TRADES_PER_DAY = "max_trades_per_day_exceeded"
    MIN_EQUITY_FLOOR = "min_equity_floor_breached"
    MANUAL_HALT = "manual_halt"  # human-triggered kill switch


@dataclass
class AccountSnapshot:
    """Minimal set of account facts needed to evaluate the breaker.
    Populated from Alpaca's /account and /positions/orders endpoints,
    or from Supabase's local trade log for today's count."""
    equity: float
    last_equity: float  # previous trading day's closing equity, from Alpaca
    open_positions_count: int
    trades_today_count: int
    manual_halt_active: bool = False

    @property
    def daily_pnl_pct(self) -> float:
        if self.last_equity == 0:
            return 0.0
        return ((self.equity - self.last_equity) / self.last_equity) * 100

    @property
    def daily_pnl_absolute(self) -> float:
        return self.equity - self.last_equity


@dataclass
class BreakerResult:
    tripped: bool
    reasons: list[TripReason]
    checked_at: datetime
    snapshot: AccountSnapshot

    @property
    def is_safe_to_proceed(self) -> bool:
        return not self.tripped


def evaluate(
    snapshot: AccountSnapshot,
    config: CircuitBreakerConfig | None = None,
) -> BreakerResult:
    """Pure function: given an account snapshot, decide whether the
    circuit breaker should be tripped. No I/O here - callers are
    responsible for fetching the snapshot and logging the result."""
    cfg = config or settings.circuit_breaker
    reasons: list[TripReason] = []

    if snapshot.manual_halt_active:
        reasons.append(TripReason.MANUAL_HALT)

    if snapshot.daily_pnl_pct <= -abs(cfg.max_daily_loss_pct):
        reasons.append(TripReason.DAILY_LOSS_PCT)

    if cfg.max_daily_loss_absolute is not None:
        if snapshot.daily_pnl_absolute <= -abs(cfg.max_daily_loss_absolute):
            reasons.append(TripReason.DAILY_LOSS_ABSOLUTE)

    if snapshot.open_positions_count >= cfg.max_open_positions:
        reasons.append(TripReason.MAX_OPEN_POSITIONS)

    if snapshot.trades_today_count >= cfg.max_trades_per_day:
        reasons.append(TripReason.MAX_TRADES_PER_DAY)

    if cfg.min_account_equity > 0 and snapshot.equity < cfg.min_account_equity:
        reasons.append(TripReason.MIN_EQUITY_FLOOR)

    return BreakerResult(
        tripped=len(reasons) > 0,
        reasons=reasons,
        checked_at=datetime.now(timezone.utc),
        snapshot=snapshot,
    )


# --- Wiring points ---

def _fetch_live_snapshot(manual_halt_active: bool = False) -> AccountSnapshot:
    """Builds a real AccountSnapshot from live Alpaca account data +
    today's trade count from Supabase. Shared by both check_pre_scan()
    and check_pre_execution() so they always evaluate against the same
    up-to-the-moment state."""
    from core.alpaca_client import get_trading_client
    from persistence.supabase_client import get_client

    trading_client = get_trading_client()
    account = trading_client.get_account()

    positions = trading_client.get_all_positions()

    # Count today's trades from our own `trades` table (UTC day boundary,
    # matching created_at's timestamptz default in the schema)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    supabase = get_client()
    trades_today = (
        supabase.table("trades")
        .select("id", count="exact")
        .gte("created_at", today_start.isoformat())
        .execute()
    )
    trades_today_count = trades_today.count or 0

    return AccountSnapshot(
        equity=float(account.equity),
        last_equity=float(account.last_equity),
        open_positions_count=len(positions),
        trades_today_count=trades_today_count,
        manual_halt_active=manual_halt_active,
    )


def _log_breaker_event(result: BreakerResult, check_point: str) -> None:
    """Persists every circuit breaker check (tripped or not) to
    circuit_breaker_events for a full audit trail, per the original
    design intent."""
    from persistence.supabase_client import get_client

    client = get_client()
    client.table("circuit_breaker_events").insert({
        "check_point": check_point,
        "tripped": result.tripped,
        "reasons": [r.value for r in result.reasons],
        "equity": result.snapshot.equity,
        "last_equity": result.snapshot.last_equity,
        "daily_pnl_pct": result.snapshot.daily_pnl_pct,
        "open_positions_count": result.snapshot.open_positions_count,
        "trades_today_count": result.snapshot.trades_today_count,
    }).execute()


def _is_manually_halted() -> bool:
    """Checks circuit_breaker_events for the most recent manual halt/clear
    action to determine if a human-triggered kill switch is currently
    active. A halt persists until a matching clear event with
    cleared_by_human=true is logged after it."""
    from persistence.supabase_client import get_client

    client = get_client()
    result = (
        client.table("circuit_breaker_events")
        .select("tripped, reasons, cleared_by_human, created_at")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )

    for row in result.data:
        if row.get("cleared_by_human"):
            return False  # most recent relevant event is a human clear
        reasons = row.get("reasons") or []
        if row.get("tripped") and "manual_halt" in reasons:
            return True

    return False


def check_pre_scan() -> BreakerResult:
    """Call this before the autonomous scan/propose loop starts. If
    tripped, the scan loop should skip its run entirely - no signals
    generated, no proposals created, nothing that could lead toward
    a trade."""
    manual_halt = _is_manually_halted()
    snapshot = _fetch_live_snapshot(manual_halt_active=manual_halt)
    result = evaluate(snapshot)
    _log_breaker_event(result, check_point="pre_scan")
    return result


def check_pre_execution() -> BreakerResult:
    """Call this immediately before ANY order submission - manual or
    auto. This is the last line of defense even if check_pre_scan
    passed earlier (state may have changed since then, e.g. a trade
    filled with a large adverse move in between)."""
    manual_halt = _is_manually_halted()
    snapshot = _fetch_live_snapshot(manual_halt_active=manual_halt)
    result = evaluate(snapshot)
    _log_breaker_event(result, check_point="pre_execution")
    return result


def trigger_manual_halt(notes: str = "") -> None:
    """Human-triggered kill switch. Logs a tripped MANUAL_HALT event;
    every subsequent check_pre_scan()/check_pre_execution() call will
    see this and stay tripped until clear_manual_halt() is called."""
    from persistence.supabase_client import get_client

    client = get_client()
    snapshot = _fetch_live_snapshot(manual_halt_active=True)
    client.table("circuit_breaker_events").insert({
        "check_point": "manual",
        "tripped": True,
        "reasons": [TripReason.MANUAL_HALT.value],
        "equity": snapshot.equity,
        "last_equity": snapshot.last_equity,
        "daily_pnl_pct": snapshot.daily_pnl_pct,
        "open_positions_count": snapshot.open_positions_count,
        "trades_today_count": snapshot.trades_today_count,
        "clear_notes": notes,
    }).execute()
    print("Manual halt triggered. Bot will not scan or execute until cleared.")


def clear_manual_halt(notes: str = "") -> None:
    """Human clears a sticky pause. This is the ONLY way execution
    resumes after a trip - there is deliberately no automatic
    timer-based resume, per the original design intent."""
    from persistence.supabase_client import get_client

    client = get_client()
    snapshot = _fetch_live_snapshot(manual_halt_active=False)
    client.table("circuit_breaker_events").insert({
        "check_point": "manual",
        "tripped": False,
        "reasons": [],
        "equity": snapshot.equity,
        "last_equity": snapshot.last_equity,
        "daily_pnl_pct": snapshot.daily_pnl_pct,
        "open_positions_count": snapshot.open_positions_count,
        "trades_today_count": snapshot.trades_today_count,
        "cleared_by_human": True,
        "cleared_at": datetime.now(timezone.utc).isoformat(),
        "clear_notes": notes,
    }).execute()
    print("Circuit breaker manually cleared. Bot may resume scanning/execution.")


if __name__ == "__main__":
    # Manual smoke test with a fake snapshot: `python -m core.circuit_breaker`
    fake = AccountSnapshot(
        equity=97000,
        last_equity=100000,
        open_positions_count=3,
        trades_today_count=5,
    )
    result = evaluate(fake)
    print(f"Tripped: {result.tripped}")
    print(f"Reasons: {[r.value for r in result.reasons]}")
    print(f"Daily P&L: {fake.daily_pnl_pct:.2f}%")
