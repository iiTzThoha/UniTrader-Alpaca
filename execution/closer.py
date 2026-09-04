"""
Closing module: takes an open `trades` row and, if the circuit breaker
allows it, submits the REVERSE order to close that position on Alpaca.

Mirrors execution/executor.py's pattern deliberately - same contract
resolution, same order submission, same circuit-breaker-before-any-
real-order discipline. Kept in a separate file (rather than folded into
executor.py) so opening and closing logic stay independently auditable,
same rationale as why executor.py is the only place orders are opened.

Reverse-side logic:
  - A trade opened with side='buy'  (long option)  closes with a SELL order.
  - A trade opened with side='sell' (short option) closes with a BUY order.

Realized P&L is computed from ACTUAL Alpaca fill prices on both legs
(open + close), not from the trades table's stored fill_price - that
column isn't currently populated on open (see orchestrator/executor
follow-up), so Alpaca's own order history is the source of truth here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from core.alpaca_client import get_trading_client
from core.circuit_breaker import check_pre_execution
from persistence.supabase_client import get_client

_REVERSE_SIDE = {"buy": "sell", "sell": "buy"}


@dataclass
class CloseResult:
    success: bool
    trade_id: str
    close_order_id: str | None
    realized_pnl: float | None
    message: str


def _get_fill_price(order_id: str) -> float | None:
    """Fetches the actual average fill price for an order from Alpaca.
    Returns None if the order hasn't filled yet (shouldn't normally
    happen for a market order but paper fills aren't instantaneous)."""
    client = get_trading_client()
    order = client.get_order_by_id(order_id)
    if order.filled_avg_price is None:
        return None
    return float(order.filled_avg_price)


def close_position(trade_row: dict) -> CloseResult:
    """Closes one open trade (one options leg) by submitting the reverse
    side order for the same contract symbol.

    Expects trade_row to have: id, symbol (OCC contract symbol),
    alpaca_order_id (the ORIGINAL opening order), side (original side),
    quantity, status.
    """
    trade_id = trade_row["id"]
    symbol = trade_row["symbol"]
    original_side = trade_row["side"]
    quantity = trade_row.get("quantity", 1)

    if trade_row.get("status") not in ("submitted", "filled"):
        return CloseResult(
            success=False, trade_id=trade_id, close_order_id=None, realized_pnl=None,
            message=f"Trade status is '{trade_row.get('status')}' - not open, skipping close.",
        )

    # Same safety gate as opening a position - account state may have
    # changed since the last circuit breaker check.
    breaker_result = check_pre_execution()
    if breaker_result.tripped:
        reasons = [r.value for r in breaker_result.reasons]
        return CloseResult(
            success=False, trade_id=trade_id, close_order_id=None, realized_pnl=None,
            message=f"Circuit breaker is tripped ({reasons}) - close blocked.",
        )

    close_side = _REVERSE_SIDE.get(original_side)
    if close_side is None:
        return CloseResult(
            success=False, trade_id=trade_id, close_order_id=None, realized_pnl=None,
            message=f"Unrecognized original side '{original_side}' - cannot determine close direction.",
        )

    client = get_trading_client()
    try:
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY if close_side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        close_order = client.submit_order(order_request)
    except Exception as e:
        return CloseResult(
            success=False, trade_id=trade_id, close_order_id=None, realized_pnl=None,
            message=f"Close order submission failed: {e}",
        )

    # Try to compute realized P&L from actual fill prices on both legs.
    # Paper fills are usually fast but not guaranteed instant, so a
    # missing fill price here isn't a failure - the close order itself
    # succeeded, P&L can be backfilled later if needed.
    open_fill = _get_fill_price(trade_row["alpaca_order_id"])
    close_fill = _get_fill_price(str(close_order.id))

    realized_pnl = None
    if open_fill is not None and close_fill is not None:
        # side='buy' at open means we PAID open_fill and RECEIVE close_fill on close.
        # side='sell' at open means we RECEIVED open_fill and PAY close_fill on close.
        if original_side == "buy":
            realized_pnl = (close_fill - open_fill) * quantity * 100
        else:
            realized_pnl = (open_fill - close_fill) * quantity * 100

    supabase = get_client()
    update_row = {
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    if realized_pnl is not None:
        update_row["realized_pnl"] = realized_pnl
    supabase.table("trades").update(update_row).eq("id", trade_id).execute()

    pnl_msg = f", realized P&L ${realized_pnl:,.2f}" if realized_pnl is not None else " (P&L pending fill confirmation)"
    return CloseResult(
        success=True, trade_id=trade_id, close_order_id=str(close_order.id),
        realized_pnl=realized_pnl,
        message=f"Closed {symbol} ({close_side} {quantity})" + pnl_msg,
    )


if __name__ == "__main__":
    # Smoke test: `python -m execution.closer`
    # WARNING: this places a REAL paper closing order on the most recently
    # opened still-open trade. Requires Alpaca + Supabase.
    client = get_client()
    result = (
        client.table("trades")
        .select("*")
        .eq("status", "submitted")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not result.data:
        print("No open ('submitted') trades found to close.")
    else:
        trade = result.data[0]
        print(f"Found open trade: {trade['symbol']} ({trade['side']} {trade['quantity']})")
        close_result = close_position(trade)
        print(f"Success: {close_result.success}")
        print(f"Message: {close_result.message}")
