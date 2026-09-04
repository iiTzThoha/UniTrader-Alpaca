"""
Automatic exit-rule evaluator: checks all open (`status='submitted'`)
trades against the standard +50% take-profit / -50% stop-loss
convention, and closes any position that has crossed either threshold.

Evaluates P&L at the PROPOSAL level, not per-leg. A multi-leg trade
(e.g. bull_call_spread) is opened as multiple `trades` rows sharing one
`proposal_id` - one leg can be up while another is down, so the only
meaningful take-profit/stop-loss signal is the combined position, not
any single leg in isolation. Single-leg trades (e.g. long_call) are
just a group of size one, so the same logic covers both cases without
a special case.

Reuses execution/closer.py's close_position() per leg - this module
only decides WHICH proposals have crossed a threshold, it never touches
order submission or Supabase writes directly (closer.py already owns
the circuit-breaker gate, the reverse-side logic, and the trades-row
update on close).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from core.alpaca_client import get_trading_client
from core.circuit_breaker import check_pre_execution
from execution.closer import close_position
from persistence.supabase_client import get_client

TAKE_PROFIT_PCT = 0.50
STOP_LOSS_PCT = -0.50


@dataclass
class ProposalEvaluation:
    proposal_id: str
    symbols: list[str]
    combined_unrealized_pnl: float
    combined_cost_basis: float
    combined_pct: float | None
    trigger: str | None  # "take_profit" | "stop_loss" | None
    closed: bool = False
    close_messages: list[str] = field(default_factory=list)


def _load_open_trades() -> list[dict]:
    client = get_client()
    result = (
        client.table("trades")
        .select("*")
        .eq("status", "submitted")
        .execute()
    )
    return result.data or []


def _group_by_proposal(trades: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in trades:
        groups[row["proposal_id"]].append(row)
    return dict(groups)


def evaluate_proposal(trade_rows: list[dict], positions_by_symbol: dict) -> ProposalEvaluation:
    """Sums unrealized $ P&L and cost basis across every leg of one
    proposal using Alpaca's own live position data (same source the
    dashboard already displays), then checks the combined % against
    the take-profit / stop-loss thresholds."""
    proposal_id = trade_rows[0]["proposal_id"]
    symbols = [r["symbol"] for r in trade_rows]

    combined_pnl = 0.0
    combined_cost_basis = 0.0
    missing_a_leg = False

    for row in trade_rows:
        pos = positions_by_symbol.get(row["symbol"])
        if pos is None:
            missing_a_leg = True
            continue
        unrealized_pl = float(pos.unrealized_pl)
        market_value = float(pos.market_value)
        cost_basis = market_value - unrealized_pl
        combined_pnl += unrealized_pl
        combined_cost_basis += abs(cost_basis)

    if missing_a_leg or combined_cost_basis == 0:
        return ProposalEvaluation(
            proposal_id=proposal_id, symbols=symbols,
            combined_unrealized_pnl=combined_pnl, combined_cost_basis=combined_cost_basis,
            combined_pct=None, trigger=None,
        )

    combined_pct = combined_pnl / combined_cost_basis

    trigger = None
    if combined_pct >= TAKE_PROFIT_PCT:
        trigger = "take_profit"
    elif combined_pct <= STOP_LOSS_PCT:
        trigger = "stop_loss"

    return ProposalEvaluation(
        proposal_id=proposal_id, symbols=symbols,
        combined_unrealized_pnl=combined_pnl, combined_cost_basis=combined_cost_basis,
        combined_pct=combined_pct, trigger=trigger,
    )


def run_exit_check() -> list[ProposalEvaluation]:
    """Main entrypoint. Evaluates every open proposal and closes any
    whose combined P&L has crossed +50%/-50%. Returns every evaluation
    (triggered or not) so the caller (dashboard button, scheduler) can
    display a full summary, not just the closures."""
    open_trades = _load_open_trades()
    if not open_trades:
        return []

    groups = _group_by_proposal(open_trades)

    client = get_trading_client()
    live_positions = client.get_all_positions()
    positions_by_symbol = {p.symbol: p for p in live_positions}

    evaluations = []
    for proposal_id, trade_rows in groups.items():
        evaluation = evaluate_proposal(trade_rows, positions_by_symbol)

        if evaluation.trigger is not None:
            breaker_result = check_pre_execution()
            if breaker_result.tripped:
                reasons = [r.value for r in breaker_result.reasons]
                evaluation.close_messages.append(
                    f"Circuit breaker tripped ({reasons}) - auto-close skipped despite {evaluation.trigger} trigger."
                )
            else:
                all_succeeded = True
                for row in trade_rows:
                    result = close_position(row)
                    evaluation.close_messages.append(result.message)
                    if not result.success:
                        all_succeeded = False
                evaluation.closed = all_succeeded

        evaluations.append(evaluation)

    return evaluations


if __name__ == "__main__":
    # Smoke test: `python -m execution.close_evaluator`
    # WARNING: this WILL place real paper closing orders on any open
    # proposal that has crossed +50%/-50%. Requires Alpaca + Supabase.
    results = run_exit_check()
    if not results:
        print("No open proposals to evaluate.")
    for ev in results:
        pct_str = f"{ev.combined_pct:+.1%}" if ev.combined_pct is not None else "N/A (missing leg data)"
        print(f"\nProposal {ev.proposal_id} ({', '.join(ev.symbols)})")
        print(f"  Combined P&L: ${ev.combined_unrealized_pnl:,.2f}  ({pct_str})")
        print(f"  Trigger: {ev.trigger or 'none'}")
        if ev.close_messages:
            print(f"  Closed: {ev.closed}")
            for msg in ev.close_messages:
                print(f"    - {msg}")
