"""
Explainability journal: turns raw proposals/signals/breaker rows into
human-readable "decision stories" - one per proposal, telling the full
arc of why the system did or didn't trade something.

Pure read layer. No new writes, no schema changes - built entirely on
top of the existing `signals`, `proposals`, and `circuit_breaker_events`
tables. The Critic's verdict is read directly off `proposals`
(critic_verdict / critic_rationale / critic_risk_score), which are
denormalized there at write time - see agents/orchestrator.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from persistence.supabase_client import get_client


def _fmt(value, decimals=2, suffix=""):
    """Small helper: format a possibly-None numeric value for prose."""
    if value is None:
        return "unavailable"
    try:
        return f"{value:.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _narrate(signal: dict, proposal: dict) -> str:
    """Builds a short, readable paragraph explaining one decision."""
    symbol = proposal.get("symbol", "?")
    strategy = proposal.get("strategy_type", "?")
    confidence = proposal.get("proposer_confidence")
    p_rationale = proposal.get("proposer_rationale", "")

    lines = []

    # --- What the signal looked like ---
    if signal:
        lines.append(
            f"**Signal snapshot** ({signal.get('timeframe', '?')} timeframe): "
            f"{symbol} was trading at ${_fmt(signal.get('underlying_price'))}. "
            f"RSI {_fmt(signal.get('rsi'), 1)}, MA trend '{signal.get('ma_trend') or 'unavailable'}', "
            f"MACD histogram {_fmt(signal.get('macd_histogram'), 3)}, ATR {_fmt(signal.get('atr'), 2)}. "
            f"IV rank {_fmt(signal.get('iv_rank'), 1)}, IV percentile {_fmt(signal.get('iv_percentile'), 1)}."
        )
    else:
        lines.append("**Signal snapshot**: original signal row not found (may have been pruned).")

    # --- What the Proposer decided ---
    lines.append(
        f"**Proposer's move**: proposed a **{strategy}** on {symbol} "
        f"with confidence {_fmt(confidence, 2)}. Reasoning: {p_rationale or 'not recorded'}"
    )

    # --- What the Critic decided ---
    verdict = proposal.get("critic_verdict")
    if verdict:
        reason = proposal.get("critic_rationale", "")
        risk_score = proposal.get("critic_risk_score")
        verdict_label = {
            "approve": "✅ Approved",
            "reject": "❌ Rejected",
            "flag": "🚩 Flagged for human review",
        }.get(verdict, verdict)
        lines.append(
            f"**Critic's verdict**: {verdict_label} (risk score {_fmt(risk_score, 2)}). {reason}"
        )
    else:
        lines.append("**Critic's verdict**: no review recorded for this proposal.")

    # --- Final outcome ---
    status = proposal.get("status", "?")
    status_label = {
        "executed": "🟢 Executed as a live paper trade",
        "approved": "🟡 Approved, awaiting execution",
        "rejected": "🔴 Rejected, no trade placed",
        "pending_review": "🟠 Flagged, awaiting human decision",
    }.get(status, status)
    lines.append(f"**Final outcome**: {status_label}")
    if proposal.get("reviewed_by"):
        lines.append(
            f"*(Reviewed by {proposal['reviewed_by']} at {proposal.get('reviewed_at', '?')})*"
        )

    return "\n\n".join(lines)


def _fetch_signal(client, signal_id: str | None) -> dict:
    if not signal_id:
        return {}
    res = client.table("signals").select("*").eq("id", signal_id).execute()
    return res.data[0] if res.data else {}


def get_decision_story(proposal_id: str) -> dict:
    """Builds one full decision story for a single proposal_id."""
    client = get_client()
    proposal_res = client.table("proposals").select("*").eq("id", proposal_id).single().execute()
    proposal = proposal_res.data or {}
    signal = _fetch_signal(client, proposal.get("signal_id"))

    return {
        "proposal": proposal,
        "signal": signal,
        "narrative": _narrate(signal, proposal),
    }


def get_recent_stories(limit: int = 25) -> list[dict]:
    """Fetches the most recent N proposals and builds a story for each.
    Used to populate the dashboard tab's dropdown and the bulk export."""
    client = get_client()
    proposals_res = (
        client.table("proposals")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    proposals = proposals_res.data or []

    stories = []
    for p in proposals:
        signal = _fetch_signal(client, p.get("signal_id"))
        stories.append({
            "proposal": p,
            "signal": signal,
            "narrative": _narrate(signal, p),
        })

    return stories


def get_recent_breaker_events(limit: int = 10) -> list[dict]:
    """Fetches recent circuit breaker events for context in the bulk report."""
    client = get_client()
    res = (
        client.table("circuit_breaker_events")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def build_markdown_report(limit: int = 25) -> str:
    """Bundles the most recent decision stories + breaker events into one
    standalone Markdown report, suitable for hackathon submission/judging."""
    stories = get_recent_stories(limit=limit)
    breaker_events = get_recent_breaker_events(limit=10)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts = [
        "# Alpaca Options Trading Agent — Decision Journal",
        f"*Generated {generated_at}*",
        "",
        "This report documents the autonomous system's most recent trading "
        "decisions: what the market signal showed, what the Proposer agent "
        "suggested, how the independent Critic agent evaluated it, and the "
        "final outcome. Each decision passes through Alpaca-sourced signal "
        "data, an LLM Proposer, an independent LLM Critic (a different "
        "model, so the system isn't checking its own work), and circuit "
        "breaker safety checks before any live paper order is placed.",
        "",
        "---",
        "",
        "## Recent Decisions",
        "",
    ]

    if not stories:
        parts.append("_No proposals recorded yet._")
    else:
        for i, story in enumerate(stories, 1):
            p = story["proposal"]
            parts.append(f"### {i}. {p.get('symbol', '?')} — {p.get('strategy_type', '?')}")
            parts.append(f"*{p.get('created_at', 'unknown time')}*")
            parts.append("")
            parts.append(story["narrative"])
            parts.append("")
            parts.append("---")
            parts.append("")

    parts.append("## Circuit Breaker Activity (most recent)")
    parts.append("")
    if not breaker_events:
        parts.append("_No breaker events recorded._")
    else:
        for ev in breaker_events:
            tripped = "🔴 TRIPPED" if ev.get("tripped") else "🟢 clear"
            parts.append(
                f"- `{ev.get('created_at', '?')}` — **{ev.get('check_point', '?')}** — {tripped}"
                + (f" — reasons: {ev.get('reasons')}" if ev.get("tripped") else "")
            )

    return "\n".join(parts)


if __name__ == "__main__":
    # Smoke test: `python -m core.journal`
    report = build_markdown_report(limit=5)
    print(report)
