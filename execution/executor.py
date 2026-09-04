"""
Execution module: takes a Critic-approved proposal and, if the circuit
breaker allows it, submits the actual options order(s) to Alpaca's
paper trading account.

This is the ONLY place in the codebase that actually calls Alpaca's
order submission endpoint - kept deliberately isolated so it's easy to
audit exactly where real (paper) orders get placed.

Safety notes:
- check_pre_execution() is called immediately before submission, even
  though the proposal already passed the Critic - account state may
  have changed since the proposal was created (e.g. another trade
  filled, a big adverse move happened).
- Multi-leg strategies are submitted as separate individual orders in
  this first implementation (true atomic multi-leg/combo orders are a
  possible future improvement, not required for defined-risk spreads
  to function correctly here since each leg is independently a valid
  options contract order).
- If ANY leg of a multi-leg proposal fails to submit, we do NOT
  attempt to place the remaining legs - a half-filled spread is a
  worse risk position than no position, so we stop and log clearly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest

from core.alpaca_client import get_trading_client
from core.circuit_breaker import check_pre_execution
from persistence.supabase_client import get_client


@dataclass
class ExecutionResult:
    success: bool
    proposal_id: str
    trade_ids: list[str]
    message: str


def _find_contract_symbol(underlying: str, strike: float, expiry: str, option_type: str) -> str | None:
    """Resolves a (strike, expiry, type) leg spec to Alpaca's actual OCC
    contract symbol.

    IMPORTANT (found via live diagnosis): Alpaca's get_option_contracts
    endpoint returns contracts ordered with the nearest expiry's full
    strike chain first. A `limit` caps the TOTAL result count, so an
    unfiltered or wide-range query can silently return ONLY near-dated
    contracts if that single expiry alone has more strikes than the
    limit (confirmed: AAPL's nearest expiry alone exceeded a 200-contract
    page). The fix is to query for an EXACT single-day expiry directly
    (cheap and precise - one day's chain is always small), rather than
    a broad date range or no filter at all.
    """
    from datetime import datetime as datetime_cls
    from datetime import timedelta

    from alpaca.trading.enums import ContractType

    client = get_trading_client()
    contract_type = ContractType.CALL if option_type == "call" else ContractType.PUT
    target_date = datetime_cls.strptime(expiry, "%Y-%m-%d").date()

    def _query_exact_day(day) -> list:
        request = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date=day,
            type=contract_type,
            strike_price_gte=str(strike),
            strike_price_lte=str(strike),
            limit=10,
        )
        response = client.get_option_contracts(request)
        return response.option_contracts if hasattr(response, "option_contracts") else response

    contracts = _query_exact_day(target_date)

    # Fall back to nearby days if the exact date isn't a valid trading/
    # expiry day (e.g. Proposer picked a weekend date)
    if not contracts:
        for offset in (1, -1, 2, -2, 3, -3):
            contracts = _query_exact_day(target_date + timedelta(days=offset))
            if contracts:
                break

    if not contracts:
        return None
    return contracts[0].symbol


def _submit_leg_order(contract_symbol: str, side: str, quantity: int) -> dict:
    """Submits a single-leg market order for one option contract."""
    client = get_trading_client()
    order_request = MarketOrderRequest(
        symbol=contract_symbol,
        qty=quantity,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(order_request)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "status": str(order.status),
        "side": side,
    }


def execute_proposal(proposal_row: dict) -> ExecutionResult:
    """Main entrypoint. Takes a stored proposal row (as returned from
    the `proposals` table, e.g. from orchestrator.run_pipeline()) and,
    if approved and the circuit breaker allows it, executes every leg.

    Expects proposal_row to have: id, symbol, status, proposed_contracts
    (list of leg dicts with side/option_type/strike/expiry/quantity).
    """
    proposal_id = proposal_row["id"]
    symbol = proposal_row["symbol"]

    if proposal_row.get("status") != "approved":
        return ExecutionResult(
            success=False, proposal_id=proposal_id, trade_ids=[],
            message=f"Proposal status is '{proposal_row.get('status')}', not 'approved' - skipping execution.",
        )

    # Final safety check, right before any real order goes out
    breaker_result = check_pre_execution()
    if breaker_result.tripped:
        reasons = [r.value for r in breaker_result.reasons]
        return ExecutionResult(
            success=False, proposal_id=proposal_id, trade_ids=[],
            message=f"Circuit breaker is tripped ({reasons}) - execution blocked.",
        )

    legs = proposal_row.get("proposed_contracts", [])
    if not legs:
        return ExecutionResult(
            success=False, proposal_id=proposal_id, trade_ids=[],
            message="Proposal has no legs to execute.",
        )

    supabase = get_client()
    trade_ids: list[str] = []

    for leg in legs:
        contract_symbol = _find_contract_symbol(
            underlying=symbol,
            strike=leg["strike"],
            expiry=leg["expiry"],
            option_type=leg["option_type"],
        )
        if contract_symbol is None:
            # Stop immediately - do not place remaining legs of a partial spread
            return ExecutionResult(
                success=False, proposal_id=proposal_id, trade_ids=trade_ids,
                message=f"Could not resolve contract for leg {leg} - halting before "
                        f"placing remaining legs to avoid a partial/unhedged position. "
                        f"{len(trade_ids)} leg(s) already submitted, if any.",
            )

        try:
            order_result = _submit_leg_order(
                contract_symbol=contract_symbol,
                side=leg["side"],
                quantity=leg.get("quantity", 1),
            )
        except Exception as e:
            return ExecutionResult(
                success=False, proposal_id=proposal_id, trade_ids=trade_ids,
                message=f"Order submission failed for leg {leg}: {e}. "
                        f"{len(trade_ids)} leg(s) already submitted, if any.",
            )

        trade_row = {
            "proposal_id": proposal_id,
            "alpaca_order_id": order_result["id"],
            "symbol": contract_symbol,
            "strategy_type": proposal_row.get("strategy_type"),
            "contracts": [leg],
            "side": leg["side"],
            "quantity": leg.get("quantity", 1),
            "status": "submitted",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "raw_order_response": order_result,
        }
        stored_trade = supabase.table("trades").insert(trade_row).execute()
        if stored_trade.data:
            trade_ids.append(stored_trade.data[0]["id"])

    # Mark proposal as executed now that all legs are in
    supabase.table("proposals").update({"status": "executed"}).eq("id", proposal_id).execute()

    return ExecutionResult(
        success=True, proposal_id=proposal_id, trade_ids=trade_ids,
        message=f"Successfully submitted {len(trade_ids)} leg(s) for proposal {proposal_id}.",
    )


if __name__ == "__main__":
    # Smoke test: `python -m execution.executor`
    # WARNING: this places a REAL paper order if it finds an approved
    # proposal with no legs already executed. Requires Alpaca + Supabase.
    from persistence.supabase_client import get_client

    client = get_client()
    result = (
        client.table("proposals")
        .select("*")
        .eq("status", "approved")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not result.data:
        print("No approved proposals found to execute. Run the orchestrator first "
              "and hope for a Critic 'approve' verdict, or check the proposals table.")
    else:
        proposal = result.data[0]
        print(f"Found approved proposal: {proposal['strategy_type']} on {proposal['symbol']}")
        exec_result = execute_proposal(proposal)
        print(f"Success: {exec_result.success}")
        print(f"Message: {exec_result.message}")
        print(f"Trade IDs: {exec_result.trade_ids}")
