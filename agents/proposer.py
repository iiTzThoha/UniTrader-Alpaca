"""
Proposer agent: takes a signal snapshot and generates a defined-risk
options trade proposal, or explicitly declines to propose anything if
the signal doesn't support a clear thesis.

Design choice: the LLM is asked to output STRICT JSON matching our
Proposal schema, and we validate it ourselves in Python (leg count,
strategy type is in our allowed enum, confidence in [0,1]) rather than
trusting the model's output blindly. An LLM proposing a trade is not
itself authorization to trade - the Critic agent and circuit breaker
still stand between this and any real order.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from agents.llm_client import call_model
from agents.types import STRATEGY_LEG_COUNT, OptionLeg, Proposal, StrategyType
from config.settings import settings
from signals.engine import FullSignal

ALLOWED_STRATEGIES = [s.value for s in StrategyType]

SYSTEM_PROMPT = f"""You are the Proposer agent in an automated options trading system.
Your job is to analyze a technical/volatility signal snapshot for one stock and either:
(a) propose ONE defined-risk options trade, or
(b) decline to propose anything if the signal doesn't support a clear thesis.

You may ONLY choose a strategy from this exact list (no naked/undefined-risk positions
are permitted in this system): {ALLOWED_STRATEGIES}

Strategy guide:
- long_call / long_put: directional bet, risk capped at premium paid. Use for strong
  directional signals when IV is NOT elevated (buying options when IV is rich is expensive).
- bull_call_spread / bear_put_spread: directional debit spreads, risk capped at net debit paid.
  Similar to long options but cheaper and with a capped upside too - good middle ground.
- bull_put_spread / bear_call_spread: directional CREDIT spreads, risk capped at (spread width -
  credit received). Best when IV Rank/Percentile is elevated (selling expensive premium) and
  you have a directional or neutral-to-directional bias.

You MUST respond with ONLY valid JSON, no other text, in exactly this shape:
{{
  "propose": true or false,
  "strategy_type": one of the allowed strategy strings (omit or null if propose is false),
  "legs": [
    {{"side": "buy" or "sell", "option_type": "call" or "put", "strike": <number>, "expiry_days_out": <integer>}}
  ],
  "rationale": "2-3 sentence explanation grounded in the specific signal values given",
  "confidence": <float between 0.0 and 1.0>
}}

If propose is false, you may set legs to an empty list and explain why in rationale
(e.g. "signal is neutral / RSI and MA trend conflict / IV rank too low for a credit strategy").
Use expiry_days_out relative to today, appropriate for the signal's timeframe.
Options typically expire on Fridays (weekly) or the third Friday of the month
(monthly) - prefer expiry_days_out values that land roughly near a Friday
(e.g. multiples of 7, like 7/14/21/30/45) rather than arbitrary day counts,
since the exact contract must actually exist on the exchange.
Strikes should be realistic relative to the given underlying_price.
"""


def _build_user_prompt(signal: FullSignal) -> str:
    return json.dumps({
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "underlying_price": signal.underlying_price,
        "rsi": signal.rsi,
        "ma_trend": signal.ma_trend,
        "macd": signal.macd,
        "macd_signal": signal.macd_signal,
        "macd_histogram": signal.macd_histogram,
        "atr": signal.atr,
        "volume": signal.volume,
        "avg_volume_20d": signal.avg_volume_20d,
        "iv_rank": signal.iv_rank,
        "iv_percentile": signal.iv_percentile,
        "hv_iv_spread": signal.hv_iv_spread,
    }, indent=2)


def _parse_llm_response(raw: str, signal: FullSignal) -> Proposal | None:
    """Parses and validates the LLM's JSON response. Returns None if the
    model declined to propose, or if the response fails validation
    (better to silently skip a bad proposal than crash or propose junk)."""
    # Strip markdown code fences if the model wrapped its JSON in them
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not data.get("propose"):
        return None

    strategy_str = data.get("strategy_type")
    if strategy_str not in ALLOWED_STRATEGIES:
        return None  # model hallucinated a disallowed strategy - reject, don't guess

    strategy_type = StrategyType(strategy_str)
    expected_legs = STRATEGY_LEG_COUNT[strategy_type]

    raw_legs = data.get("legs", [])
    if len(raw_legs) != expected_legs:
        return None  # leg count mismatch - malformed, reject rather than coerce

    legs = []
    today = date.today()
    for leg in raw_legs:
        try:
            days_out = int(leg["expiry_days_out"])
            expiry = (today + timedelta(days=days_out)).isoformat()
            legs.append(OptionLeg(
                side=leg["side"],
                option_type=leg["option_type"],
                strike=float(leg["strike"]),
                expiry=expiry,
            ))
        except (KeyError, ValueError, TypeError):
            return None

    confidence = data.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (ValueError, TypeError):
        confidence = 0.0

    return Proposal(
        symbol=signal.symbol,
        strategy_type=strategy_type,
        timeframe=signal.timeframe,
        legs=legs,
        rationale=data.get("rationale", ""),
        confidence=confidence,
    )


def generate_proposal(signal: FullSignal) -> Proposal | None:
    """Main entrypoint. Returns a validated Proposal, or None if the
    Proposer declined or its output failed validation."""
    user_prompt = _build_user_prompt(signal)
    raw_response = call_model(
        model=settings.proposer_model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.3,
    )
    return _parse_llm_response(raw_response, signal)


if __name__ == "__main__":
    # Smoke test: `python -m agents.proposer`
    # Requires AIML_API_KEY, and pulls a real signal via signals.engine
    # (which itself needs Alpaca + Supabase credentials).
    from signals.engine import build_signal

    signal = build_signal("AAPL")
    print(f"Signal for {signal.symbol}: RSI={signal.rsi:.1f}, trend={signal.ma_trend}, "
          f"IV rank={signal.iv_rank}, HV-IV spread={signal.hv_iv_spread}")

    proposal = generate_proposal(signal)
    if proposal is None:
        print("Proposer declined to propose a trade for this signal.")
    else:
        print(f"\nProposal: {proposal.strategy_type.value} on {proposal.symbol}")
        print(f"Confidence: {proposal.confidence}")
        print(f"Rationale: {proposal.rationale}")
        for leg in proposal.legs:
            print(f"  Leg: {leg.to_dict()}")
