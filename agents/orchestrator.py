"""
Orchestrates the full Day 3 pipeline for one symbol:
  signal -> Proposer -> Critic -> persist to `proposals` + `reviews_rejections`

This is the main entrypoint Day 4's scheduler will call per symbol in
the watchlist. Kept separate from proposer.py/critic.py so those stay
independently testable.

Lifecycle status mapping (per the `proposals` table's check constraint):
  - Proposer declines               -> no row written at all (nothing to store)
  - Critic verdict 'approve'        -> status = 'approved'
  - Critic verdict 'reject'         -> status = 'rejected'
  - Critic verdict 'flag'           -> status = 'pending_review' (needs a human look)

Note: this function does NOT execute any trade and does NOT check the
circuit breaker - execution is a separate, later step (Day 4+), and
even an 'approved' status here is not itself authorization to trade.
"""

from __future__ import annotations

from dataclasses import asdict

from agents.critic import review_proposal
from agents.proposer import generate_proposal
from agents.types import Proposal
from config.settings import ExecutionMode, settings
from persistence.supabase_client import get_client
from signals.engine import FullSignal, build_signal, store_signal

_VERDICT_TO_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "flag": "pending_review",
}

def _compute_quantity(confidence: float, risk_score: float) -> int:
    """Deterministic, capped position sizing based on Proposer confidence
    and Critic risk_score - not LLM-driven, since contract count is a
    numeric risk decision rather than a judgment call. Kept intentionally
    small (options leverage means 1 contract ~= 100 shares of exposure) -
    this is a simple size-with-conviction rule, not a full risk-per-trade
    or Kelly-criterion engine."""
    if risk_score >= 0.6:
        return 1
    if confidence >= 0.75 and risk_score <= 0.3:
        return 3
    if confidence >= 0.6:
        return 2
    return 1

def _persist_proposal(
    signal: FullSignal,
    signal_row_id: str,
    proposal: Proposal,
    review,
) -> dict:
    client = get_client()

    proposal_row = {
        "signal_id": signal_row_id,
        "symbol": proposal.symbol,
        "strategy_type": proposal.strategy_type.value,
        "timeframe": proposal.timeframe,
        "proposer_rationale": proposal.rationale,
        "proposer_confidence": proposal.confidence,
        "proposed_contracts": [
            {**leg.to_dict(), "quantity": _compute_quantity(proposal.confidence, review.risk_score)}
            for leg in proposal.legs
        ],
        "critic_verdict": review.verdict,
        "critic_rationale": review.rationale,
        "critic_risk_score": review.risk_score,
        "status": _VERDICT_TO_STATUS[review.verdict],
        "execution_mode_at_creation": settings.execution_mode.value,
    }
    result = client.table("proposals").insert(proposal_row).execute()
    stored_proposal = result.data[0] if result.data else {}

    # Also log to reviews_rejections for full audit history, independent
    # of the proposals.status field (which can change later on re-review)
    review_row = {
        "proposal_id": stored_proposal.get("id"),
        "reviewer_type": "auto_critic",
        "decision": review.verdict,
        "reason": review.rationale,
        "rejection_category": (
            ",".join(review.concerns) if review.verdict == "reject" and review.concerns else None
        ),
    }
    client.table("reviews_rejections").insert(review_row).execute()

    return stored_proposal




def run_pipeline(symbol: str, timeframe=None) -> dict | None:
    """Full pipeline for one symbol: build signal -> store it -> propose ->
    critique -> persist -> route based on verdict:
      - 'approve' in AUTO mode -> immediately attempt execution
      - 'approve' in MANUAL mode -> left as 'approved', awaiting human action
      - 'flag' -> status = 'pending_review', surfaced in dashboard Review Queue
      - 'reject' -> stored for audit, no further action

    Returns the stored proposal dict (reflecting final status after any
    execution attempt), or None if the Proposer declined."""
    from config.settings import Timeframe
    tf = timeframe or Timeframe.MEDIUM

    signal = build_signal(symbol, tf)
    stored_signal = store_signal(signal)
    signal_row_id = stored_signal.get("id")

    proposal = generate_proposal(signal)
    if proposal is None:
        print(f"[{symbol}] Proposer declined - no clear thesis from current signal.")
        return None

    review = review_proposal(signal, proposal)
    stored_proposal = _persist_proposal(signal, signal_row_id, proposal, review)

    print(f"[{symbol}] {proposal.strategy_type.value} -> Critic: {review.verdict} "
          f"(risk {review.risk_score}) -> status: {stored_proposal.get('status')}")

    if review.verdict == "flag":
        print(f"[{symbol}] Flagged - status set to pending_review, awaiting human review in dashboard.")

    elif review.verdict == "approve" and settings.execution_mode == ExecutionMode.AUTO:
        from execution.executor import execute_proposal
        exec_result = execute_proposal(stored_proposal)
        print(f"[{symbol}] Auto-execution: {exec_result.message}")
        if exec_result.success:
            stored_proposal["status"] = "executed"

    return stored_proposal


if __name__ == "__main__":
    # Smoke test: `python -m agents.orchestrator`
    # Requires Alpaca + Supabase + AIML_API_KEY all configured.
    result = run_pipeline("AAPL")
    if result:
        print("\nFull stored proposal:")
        for k, v in result.items():
            print(f"  {k}: {v}")
