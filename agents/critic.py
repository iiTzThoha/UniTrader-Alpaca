"""
Critic agent: independently reviews a Proposal for risk before it can
reach execution. Uses a DIFFERENT underlying model than the Proposer
(configured via settings.critic_model) so the review isn't just the
same model checking its own work.

The Critic does NOT re-generate a trade idea - it only evaluates the
one it's given, and must ground its verdict in the actual signal data,
not just the Proposer's stated rationale (a Proposer could describe a
weak setup persuasively; the Critic is given the raw signal too so it
can independently judge whether the rationale holds up).
"""

from __future__ import annotations

import json
from datetime import date

from agents.llm_client import call_model
from agents.types import CriticReview, Proposal
from config.settings import settings
from signals.engine import FullSignal

SYSTEM_PROMPT = """You are the Critic agent in an automated options trading system.
Independently assess the given trade proposal's risk/soundness against its signal
data - you are the last checkpoint before human review or autonomous execution.
Do not trust the Proposer's rationale blindly; verify it against the signal values.

Evaluate:
- Direction/strategy match: does the strategy fit RSI/MA trend/MACD, or is the
  Proposer cherry-picking one indicator while ignoring conflicting ones?
- IV Rank/Percentile fit, WHEN AVAILABLE (credit spreads suit elevated IV; buying
  premium is costly when IV is rich). iv_rank/iv_percentile are often null early in
  this system's life (insufficient history yet) - this is EXPECTED, not a red flag.
  Judge on other merits (technicals, hv_iv_spread, strike/ATR sanity) instead; never
  flag/reject solely for missing IV history.
- Strike/expiry sanity vs. underlying price and ATR (a tight strike relative to ATR
  is riskier than it looks). Use the given "today" and each leg's "days_to_expiry"
  for time horizon - never estimate DTE yourself from the expiry string.
- Whether the Proposer's stated confidence is justified, inflated, or understated.

Respond with ONLY valid JSON, no other text:
{
  "verdict": "approve" or "reject" or "flag",
  "rationale": "2-4 sentence independent assessment grounded in the signal data",
  "risk_score": <float 0.0 (safe) to 1.0 (risky)>,
  "concerns": ["short phrase", ...] or []
}

"flag": a genuine, SPECIFIC concern worth human attention - not just missing optional
data. "reject": rationale doesn't hold up, or risk too high for the strategy. "approve":
reasonably justified, no specific concrete concern - doesn't need every data point
present. All strategies are already defined-risk (max loss capped) - reflect that
structural safety net in risk_score rather than treating trades as unlimited-downside.
"""


def _build_user_prompt(signal: FullSignal, proposal: Proposal) -> str:
    today = date.today()

    def _leg_with_dte(leg):
        d = leg.to_dict()
        try:
            expiry_date = date.fromisoformat(d["expiry"])
            d["days_to_expiry"] = (expiry_date - today).days
        except (KeyError, ValueError):
            d["days_to_expiry"] = None
        return d

    return json.dumps({
        "today": today.isoformat(),
        "signal": {
            "symbol": signal.symbol,
            "underlying_price": signal.underlying_price,
            "rsi": signal.rsi,
            "ma_trend": signal.ma_trend,
            "macd": signal.macd,
            "macd_signal": signal.macd_signal,
            "macd_histogram": signal.macd_histogram,
            "atr": signal.atr,
            "iv_rank": signal.iv_rank,
            "iv_percentile": signal.iv_percentile,
            "hv_iv_spread": signal.hv_iv_spread,
        },
        "proposal": {
            "strategy_type": proposal.strategy_type.value,
            "legs": [_leg_with_dte(leg) for leg in proposal.legs],
            "rationale": proposal.rationale,
            "confidence": proposal.confidence,
        },
    }, indent=2)

def _parse_llm_response(raw: str) -> CriticReview:
    """Parses the Critic's JSON response. Unlike the Proposer, a Critic
    response that fails to parse should NOT silently disappear - that
    would let a bad proposal through by default. We fail closed: an
    unparseable Critic response is treated as a 'reject' with a note
    explaining why, rather than allowing the proposal through."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        verdict = data.get("verdict")
        if verdict not in ("approve", "reject", "flag"):
            raise ValueError("invalid verdict value")

        risk_score = max(0.0, min(1.0, float(data.get("risk_score", 1.0))))

        return CriticReview(
            verdict=verdict,
            rationale=data.get("rationale", ""),
            risk_score=risk_score,
            concerns=data.get("concerns", []),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # Fail closed: malformed Critic output blocks the trade rather than
        # letting it through silently.
        return CriticReview(
            verdict="reject",
            rationale=f"Critic response could not be parsed/validated ({e}); "
                      "failing closed and rejecting this proposal.",
            risk_score=1.0,
            concerns=["critic_output_malformed"],
        )


def review_proposal(signal: FullSignal, proposal: Proposal) -> CriticReview:
    """Main entrypoint: independently reviews a Proposal against its
    originating signal, using a different model than the Proposer."""
    user_prompt = _build_user_prompt(signal, proposal)
    raw_response = call_model(
        model=settings.critic_model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.2,  # slightly lower temp - critic should be more consistent
    )
    return _parse_llm_response(raw_response)


if __name__ == "__main__":
    # Smoke test: `python -m agents.critic`
    # Requires AIML_API_KEY, Alpaca + Supabase credentials (via signal + proposer).
    from agents.proposer import generate_proposal
    from signals.engine import build_signal

    signal = build_signal("AAPL")
    proposal = generate_proposal(signal)

    if proposal is None:
        print("Proposer declined - nothing for the Critic to review. "
              "Try a different symbol or wait for a clearer signal.")
    else:
        print(f"Proposal to review: {proposal.strategy_type.value} on {proposal.symbol} "
              f"(confidence {proposal.confidence})")
        review = review_proposal(signal, proposal)
        print(f"\nCritic verdict: {review.verdict}")
        print(f"Risk score: {review.risk_score}")
        print(f"Rationale: {review.rationale}")
        print(f"Concerns: {review.concerns}")
