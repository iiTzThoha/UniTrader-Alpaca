"""
Day 5 — Dashboard.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from config.settings import settings
from core.circuit_breaker import check_pre_scan, clear_manual_halt, trigger_manual_halt
from core.journal import get_recent_stories, build_markdown_report
from execution.executor import execute_proposal
from execution.closer import close_position
from execution.close_evaluator import run_exit_check
from persistence.supabase_client import get_client
from signals.universe import build_watchlist

st.set_page_config(page_title="UniTrader", layout="wide", page_icon="📈")

auto_refresh_on = st.sidebar.checkbox("Auto-refresh (5s)", value=False, key="auto_refresh_toggle")
if auto_refresh_on:
    st_autorefresh(interval=5000, key="dashboard_autorefresh")

st.markdown("""
<style>
.mono-num {
    font-family: "SF Mono", "Roboto Mono", Consolas, monospace;
    font-variant-numeric: tabular-nums;
}

.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.02em;
}
.badge-green  { background: rgba(63, 185, 80, 0.15);  color: #3FB950; border: 1px solid rgba(63, 185, 80, 0.35); }
.badge-red    { background: rgba(248, 81, 73, 0.15);  color: #F85149; border: 1px solid rgba(248, 81, 73, 0.35); }
.badge-yellow { background: rgba(210, 153, 34, 0.15); color: #D29922; border: 1px solid rgba(210, 153, 34, 0.35); }
.badge-grey   { background: rgba(139, 148, 158, 0.15);color: #8B949E; border: 1px solid rgba(139, 148, 158, 0.35); }
.badge-blue   { background: rgba(168, 85, 247, 0.15); color: #A855F7; border: 1px solid rgba(168, 85, 247, 0.35); }

.proposal-card {
    background: #161B22;
    border: 1px solid #30363D;
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 12px;
    transition: border-color 0.2s ease;
}
.proposal-card:hover {
    border-color: #A855F7;
}

.badge-approve {
    background: rgba(34, 197, 94, 0.15);
    color: #22C55E;
    border: 1px solid rgba(34, 197, 94, 0.3);
}
.badge-flag {
    background: rgba(245, 158, 11, 0.15);
    color: #F59E0B;
    border: 1px solid rgba(245, 158, 11, 0.3);
}
.badge-reject {
    background: rgba(239, 68, 68, 0.15);
    color: #EF4444;
    border: 1px solid rgba(239, 68, 68, 0.3);
}
.badge-pending {
    background: rgba(99, 102, 241, 0.15);
    color: #818CF8;
    border: 1px solid rgba(99, 102, 241, 0.3);
}

.confidence-bar {
    display: inline-block;
    width: 80px;
    height: 4px;
    background: #1C2128;
    border-radius: 2px;
    overflow: hidden;
    vertical-align: middle;
}
.confidence-bar .fill {
    height: 100%;
    border-radius: 2px;
    background: linear-gradient(90deg, #A855F7, #7C3AED);
    transition: width 0.3s;
}
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=5)
def load_table(name: str, order_col: str = "created_at", limit: int = 200) -> pd.DataFrame:
    client = get_client()
    result = (
        client.table(name)
        .select("*")
        .order(order_col, desc=True)
        .limit(limit)
        .execute()
    )
    return pd.DataFrame(result.data)

def get_account_snapshot():
    from core.alpaca_client import get_trading_client
    client = get_trading_client()
    account = client.get_account()
    positions = client.get_all_positions()
    return account, positions

def refresh_all():
    load_table.clear()
    st.rerun()

# =========================================================================
# HEADER: UniTrader Branding
# =========================================================================
st.markdown('<h1 style="color: #A855F7; font-weight: 700;">UniTrader</h1>', unsafe_allow_html=True)
st.markdown('<p style="color: #C9D1D9; font-size: 16px; margin-top: -10px;">Autonomous AI Trading Agent · Alpaca Paper API</p>', unsafe_allow_html=True)
st.markdown('<div style="height: 2px; background: linear-gradient(90deg, #A855F7, #7C3AED, #A855F7); margin: 8px 0 16px 0; border-radius: 2px;"></div>', unsafe_allow_html=True)

# =========================================================================
# METRICS + CHART (Side by Side)
# =========================================================================
left_col, right_col = st.columns([1, 2])

with left_col:
    try:
        account, positions = get_account_snapshot()
        equity = float(account.equity)
        buying_power = float(account.buying_power)
        last_equity = float(account.last_equity) if hasattr(account, 'last_equity') and account.last_equity else equity
        daily_pnl = equity - last_equity
        daily_pnl_pct = (daily_pnl / last_equity * 100) if last_equity else 0.0
        total_pnl = equity - 100000
        total_pnl_pct = (total_pnl / 100000 * 100) if total_pnl else 0.0

        st.metric("Portfolio Value", f"${equity:,.2f}", f"{daily_pnl:+,.2f} ({daily_pnl_pct:+.2f}%)")
        st.metric("Daily P&L", f"{daily_pnl:+,.2f}", f"{daily_pnl_pct:+.2f}% Today")
        st.metric("Total Return", f"{total_pnl:+,.2f}", f"{total_pnl_pct:+.2f}% Return")
        st.metric("Buying Power", f"${buying_power:,.2f}", f"{len(positions)} Open Positions")
    except Exception as e:
        st.error(f"Could not fetch live account data: {e}")

with right_col:
    try:
        import plotly.graph_objects as go
        import yfinance as yf

        # Chart symbol input
        chart_symbol_input = st.text_input(
            "Chart Symbol",
            value=st.session_state.get("chart_symbol", ""),
            placeholder="e.g. AAPL, MSFT, NVDA",
            key="chart_symbol_input_unique"
        )
        if chart_symbol_input:
            symbol_upper = chart_symbol_input.strip().upper()
            if symbol_upper:
                st.session_state["chart_symbol"] = symbol_upper

        # Get symbol and timeframe from session state
        chart_symbol = st.session_state.get("chart_symbol", "")
        chart_period = st.session_state.get("chart_period", "Today")
        
        # Map period to yfinance format
        period_map = {
            "Today": "1d",
            "Week": "5d",
            "Month": "1mo"
        }
        interval_map = {
            "1d": "5m",
            "5d": "30m",
            "1mo": "1d"
        }
        yf_period = period_map.get(chart_period, "1d")
        yf_interval = interval_map.get(yf_period, "5m")
        
        # Only fetch data if symbol is provided
        if chart_symbol:
            ticker = yf.Ticker(chart_symbol)
            hist = ticker.history(period=yf_period, interval=yf_interval)
        else:
            hist = None

        # Timeframe selector buttons
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            if st.button("Today", use_container_width=True, key="btn_today"):
                st.session_state["chart_period"] = "Today"
                st.rerun()
        with col_b:
            if st.button("Week", use_container_width=True, key="btn_week"):
                st.session_state["chart_period"] = "Week"
                st.rerun()
        with col_c:
            if st.button("Month", use_container_width=True, key="btn_month"):
                st.session_state["chart_period"] = "Month"
                st.rerun()



        if chart_symbol and not hist.empty:
            st.markdown(f'<p style="color: #8B949E; font-size: 14px; margin-bottom: 4px;">{chart_symbol} - {chart_period}</p>', unsafe_allow_html=True)
            
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=hist.index,
                y=hist['Close'],
                mode='lines',
                name=chart_symbol,
                line=dict(color='#A855F7', width=2)
            ))
            fig.update_layout(
                template='plotly_dark',
                height=350,
                margin=dict(l=0, r=0, t=10, b=10),
                xaxis=dict(gridcolor='#1C2128', title=None),
                yaxis=dict(gridcolor='#1C2128', title=None),
                plot_bgcolor='#0D1117',
                paper_bgcolor='#0D1117',
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
            
            # Show latest price
            latest = hist['Close'].iloc[-1]
            first = hist['Close'].iloc[0]
            change = ((latest / first - 1) * 100)
            color = "#22C55E" if change >= 0 else "#EF4444"
            st.markdown(f'<span style="font-size:20px; font-weight:600; color:#F0F6FC;">${latest:.2f}</span> <span style="color:{color};">{change:+.2f}%</span>', unsafe_allow_html=True)
        elif not chart_symbol:
            st.info("Enter a symbol above to view chart")
        else:
            st.info(f"No data for {chart_symbol}")
    except Exception as e:
        st.caption(f"Chart unavailable: {e}")

# =========================================================================
# SAFETY CONTROLS (Sidebar)
# =========================================================================
st.sidebar.divider()
st.sidebar.markdown('<h3 style="color: #A855F7;">Safety Controls</h3>', unsafe_allow_html=True)

st.sidebar.markdown("**Circuit Breaker**")
st.sidebar.caption("Checks account equity, daily loss, and open positions.")
if st.sidebar.button("Check status", use_container_width=True):
    with st.spinner("Checking..."):
        result = check_pre_scan()
    if result.tripped:
        reasons = ", ".join(r.value.replace("_", " ") for r in result.reasons)
        st.sidebar.markdown(f'<span class="badge badge-red">TRIPPED</span> {reasons}', unsafe_allow_html=True)
    else:
        st.sidebar.markdown('<span class="badge badge-green">OK — safe to trade</span>', unsafe_allow_html=True)

st.sidebar.divider()
st.sidebar.markdown("**Kill Switch**")
col_halt, col_resume = st.sidebar.columns(2)
with col_halt:
    if st.button("Halt", use_container_width=True, type="secondary"):
        trigger_manual_halt(notes="Triggered from dashboard")
        st.sidebar.warning("Trading halted.")
        refresh_all()
with col_resume:
    if st.button("Resume", use_container_width=True, type="secondary"):
        clear_manual_halt(notes="Cleared from dashboard")
        st.sidebar.success("Halt cleared.")
        refresh_all()

st.sidebar.divider()
if st.sidebar.button("Force Refresh", use_container_width=True):
    refresh_all()
st.sidebar.caption("Last updated: " + datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))

# =========================================================================
# SCAN PANEL (SEPARATE FROM CHART)
# =========================================================================
st.divider()

with st.container(border=True):
    st.markdown('<h3 style="color: #A855F7;">Scan for Trade Ideas</h3>', unsafe_allow_html=True)
    st.caption("Analyzes symbols and generates trade proposals. Checks circuit breaker first.")

    scan_col1, scan_col2 = st.columns([3, 1])
    with scan_col1:
        scan_symbols_input = st.text_input(
            "Symbols to scan (comma-separated)",
            value="",
            placeholder="e.g. AAPL, MSFT, TSLA",
            key="scan_symbols_input"
        )
        scan_symbols = [s.strip().upper() for s in scan_symbols_input.split(",") if s.strip()]
        if not scan_symbols:
            scan_symbols = ["AAPL"]
    with scan_col2:
        use_full_watchlist = st.checkbox("Use full watchlist", value=False)

    if use_full_watchlist:
        st.warning(f"Execution mode is **{settings.execution_mode.value.upper()}**.")
        confirm_full_scan = st.checkbox("I understand", key="confirm_full_scan")
    else:
        confirm_full_scan = True

    if st.button("Run scan now", disabled=use_full_watchlist and not confirm_full_scan, type="primary"):
        symbols_to_scan = build_watchlist() if use_full_watchlist else scan_symbols
        scan_label = "full watchlist" if use_full_watchlist else ", ".join(symbols_to_scan)

        breaker_result = check_pre_scan()
        if breaker_result.tripped:
            st.error("Trading is halted — scan skipped.")
        else:
            st.session_state.last_scan_outcomes = []
            progress = st.progress(0.0, text=f"Scanning {scan_label}...")
            from agents.orchestrator import run_pipeline

            for i, symbol in enumerate(symbols_to_scan):
                progress.progress((i) / len(symbols_to_scan), text=f"Scanning {symbol}...")
                try:
                    result = run_pipeline(symbol)
                    if result is None:
                        st.session_state.last_scan_outcomes.append((symbol, "declined", "No clear trade setup found."))
                    else:
                        verdict = result.get("critic_verdict", "?")
                        status = result.get("status", "?")
                        st.session_state.last_scan_outcomes.append(
                            (symbol, "proposal", f"verdict: {verdict}, status: {status}")
                        )
                except Exception as e:
                    st.session_state.last_scan_outcomes.append((symbol, "error", f"{type(e).__name__}: {e}"))

            progress.progress(1.0, text="Scan complete.")

        if "last_scan_outcomes" in st.session_state and st.session_state.last_scan_outcomes:
            n_proposals = sum(1 for _, kind, _ in st.session_state.last_scan_outcomes if kind == "proposal")
            n_declined = sum(1 for _, kind, _ in st.session_state.last_scan_outcomes if kind == "declined")
            n_errors = sum(1 for _, kind, _ in st.session_state.last_scan_outcomes if kind == "error")
            st.success(f"Scan complete: {n_proposals} proposals, {n_declined} declined, {n_errors} errors.")
            for symbol, kind, detail in st.session_state.last_scan_outcomes:
                icon = {"proposal": "✅", "declined": "⚪", "error": "🔴"}[kind]
                st.write(f"{icon} **{symbol}** — {detail}")

# =========================================================================
# EXIT CHECK
# =========================================================================
st.divider()

with st.container(border=True):
    st.markdown('<h3 style="color: #A855F7;">Check Exits</h3>', unsafe_allow_html=True)
    st.caption("Evaluates open positions against +50% take-profit / -50% stop-loss.")
    if st.button("Check exits now", type="primary"):
        with st.spinner("Evaluating..."):
            evaluations = run_exit_check()
        if not evaluations:
            st.info("No open positions.")
        else:
            for ev in evaluations:
                pct_str = f"{ev.combined_pct:+.1%}" if ev.combined_pct is not None else "N/A"
                label = ", ".join(ev.symbols)
                if ev.trigger is None:
                    st.write(f"**{label}** — ${ev.combined_unrealized_pnl:,.2f} ({pct_str}) — no trigger")
                elif ev.closed:
                    st.success(f"**{label}** — {ev.trigger} hit at {pct_str} — closed.")
                else:
                    st.error(f"**{label}** — {ev.trigger} hit at {pct_str} — close FAILED.")
            load_table.clear()

st.divider()

# =========================================================================
# TABS
# =========================================================================
tab_review, tab_proposals, tab_trades, tab_breaker_log, tab_journal = st.tabs(
    ["Review Queue", "All Proposals", "Trades", "Breaker Log", "Decision Journal"]
)

# ---------------------------------------------------------------------
# REVIEW QUEUE
# ---------------------------------------------------------------------
def _bulk_reject(proposal_ids: list[str], reason: str) -> tuple[int, int]:
    client = get_client()
    ok, fail = 0, 0
    for pid in proposal_ids:
        try:
            client.table("proposals").update(
                {"status": "rejected", "reviewed_at": datetime.now(timezone.utc).isoformat(), "reviewed_by": "human"}
            ).eq("id", pid).execute()
            client.table("reviews_rejections").insert(
                {"proposal_id": pid, "reviewer_type": "human", "decision": "reject", "reason": reason, "rejection_category": "other"}
            ).execute()
            ok += 1
        except Exception:
            fail += 1
    return ok, fail

def _bulk_approve_and_execute(proposal_ids: list[str]) -> list[tuple[str, bool, str]]:
    client = get_client()
    results = []
    for pid in proposal_ids:
        client.table("proposals").update(
            {"status": "approved", "reviewed_at": datetime.now(timezone.utc).isoformat(), "reviewed_by": "human"}
        ).eq("id", pid).execute()
        client.table("reviews_rejections").insert(
            {"proposal_id": pid, "reviewer_type": "human", "decision": "approve", "reason": "Approved via dashboard"}
        ).execute()
        fresh = client.table("proposals").select("*").eq("id", pid).single().execute()
        exec_result = execute_proposal(fresh.data)
        results.append((pid, exec_result.success, exec_result.message))
        if not exec_result.success and "circuit breaker" in exec_result.message.lower():
            break
    return results

with tab_review:
    st.caption("Proposals awaiting a human decision. Approving places a REAL paper order.")
    proposals_df = load_table("proposals", limit=300)

    if proposals_df.empty:
        st.info("No proposals found yet.")
    else:
        review_df = proposals_df[
            proposals_df["status"].isin(["pending_review", "wishlisted"]) | (proposals_df["critic_verdict"] == "flag")
        ].copy()
        review_df = review_df[~review_df["status"].isin(["executed", "rejected", "expired"])]

        if review_df.empty:
            st.success("Review queue is empty.")
        else:
            st.write(f"**{len(review_df)} proposal(s)** waiting for review")

            if "selected_proposal_ids" not in st.session_state:
                st.session_state.selected_proposal_ids = set()

            sel_col1, sel_col2, sel_col3 = st.columns([1, 1, 2])
            with sel_col1:
                if st.button("Select all shown"):
                    for pid in review_df["id"].tolist():
                        st.session_state[f"chk_{pid}"] = True
                    st.rerun()
            with sel_col2:
                if st.button("Clear selection"):
                    for pid in review_df["id"].tolist():
                        st.session_state[f"chk_{pid}"] = False
                    st.rerun()

            st.session_state.selected_proposal_ids = {
                pid for pid in review_df["id"].tolist() if st.session_state.get(f"chk_{pid}", False)
            }
            n_selected = len(st.session_state.selected_proposal_ids)
            with sel_col3:
                st.write(f"**{n_selected} selected**")

            bulk_col1, bulk_col2 = st.columns(2)
            with bulk_col1:
                bulk_reject_reason = st.text_input("Bulk rejection reason", value="Stale", key="bulk_reject_reason")
                if st.button(f"Reject selected ({n_selected})", disabled=n_selected == 0):
                    ids = list(st.session_state.selected_proposal_ids)
                    ok, fail = _bulk_reject(ids, bulk_reject_reason)
                    st.warning(f"Rejected {ok} proposal(s)." + (f" {fail} failed." if fail else ""))
                    for pid in ids:
                        st.session_state[f"chk_{pid}"] = False
                    st.session_state.selected_proposal_ids = set()
                    refresh_all()

            with bulk_col2:
                confirm_bulk_approve = st.checkbox(
                    f"I understand this places {n_selected} REAL paper order(s)", key="confirm_bulk_approve", disabled=n_selected == 0
                )
                if st.button(f"Approve & Execute selected ({n_selected})", disabled=n_selected == 0 or not confirm_bulk_approve):
                    ids = list(st.session_state.selected_proposal_ids)
                    results = _bulk_approve_and_execute(ids)
                    for pid, success, message in results:
                        if success:
                            st.success(f"{pid[:8]}… — {message}")
                        else:
                            st.error(f"{pid[:8]}… — {message}")
                    for pid in ids:
                        st.session_state[f"chk_{pid}"] = False
                    st.session_state.selected_proposal_ids = set()
                    refresh_all()

            st.divider()

            for _, row in review_df.iterrows():
                verdict = row.get("critic_verdict") or "pending"
                risk = row.get("critic_risk_score")
                risk_str = f"{risk:.2f}" if risk is not None else "—"
                confidence = row.get("proposer_confidence", 0)
                conf_pct = int(confidence * 100) if confidence else 0
                symbol = row.get("symbol", "N/A")
                strategy = row.get("strategy_type", "Unknown")
                rationale = row.get("critic_rationale") or row.get("proposer_rationale") or "No rationale provided"
                if len(rationale) > 200:
                    rationale = rationale[:200] + "..."

                if verdict.lower() == "approve":
                    badge_class = "badge-approve"
                    badge_text = "Approved"
                elif verdict.lower() == "flag":
                    badge_class = "badge-flag"
                    badge_text = "Flagged"
                elif verdict.lower() == "reject":
                    badge_class = "badge-reject"
                    badge_text = "Rejected"
                else:
                    badge_class = "badge-pending"
                    badge_text = "Pending"

                risk_color = "#22C55E" if risk and risk < 0.3 else "#F59E0B" if risk and risk < 0.6 else "#EF4444"

                st.markdown(f'''
                <div class="proposal-card">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
                        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
                            <span style="font-size:18px; font-weight:700; color:#F0F6FC;">{symbol}</span>
                            <span class="badge {badge_class}">{badge_text}</span>
                            <span class="badge badge-blue" style="font-size:10px;">{strategy}</span>
                        </div>
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span style="color:#8B949E; font-size:12px; text-transform:uppercase;">Risk</span>
                            <span style="color:{risk_color}; font-weight:600;">{risk_str}</span>
                        </div>
                    </div>
                    <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px; flex-wrap:wrap;">
                        <span style="color:#8B949E; font-size:12px; text-transform:uppercase;">Confidence</span>
                        <span style="color:#F0F6FC; font-weight:500; font-size:14px;">{conf_pct}%</span>
                        <div class="confidence-bar" style="flex:1; min-width:60px;">
                            <div class="fill" style="width:{conf_pct}%;"></div>
                        </div>
                    </div>
                    <div style="color:#C9D1D9; font-size:13px; line-height:1.5; padding:8px 12px; background:#0D1117; border-radius:6px; border-left:2px solid #30363D;">
                        {rationale}
                    </div>
                </div>
                ''', unsafe_allow_html=True)

                checkbox_col, expander_col = st.columns([1, 8])
                with checkbox_col:
                    st.checkbox("", key=f"chk_{row['id']}", label_visibility="collapsed")
                with expander_col:
                    with st.expander(f"Details — {symbol} — {strategy}"):
                        left, right = st.columns([2, 1])
                        with left:
                            st.markdown(f"**Proposer rationale:** {row.get('proposer_rationale') or '—'}")
                            st.markdown(f"**Proposer confidence:** {row.get('proposer_confidence')}")
                            st.markdown(f"**Critic rationale:** {row.get('critic_rationale') or '—'}")
                            st.markdown("**Legs:**")
                            st.json(row.get("proposed_contracts") or [])
                        with right:
                            st.markdown(f"**Created:** {row.get('created_at')}")
                            st.markdown(f"**Timeframe:** {row.get('timeframe')}")
                            st.markdown(f"**Status:** {row.get('status')}")
                            st.markdown(f"**Execution mode:** {row.get('execution_mode_at_creation')}")

                        btn_col1, btn_col2 = st.columns(2)
                        with btn_col1:
                            if st.button("Approve & Execute", key=f"approve_{row['id']}"):
                                client = get_client()
                                client.table("proposals").update(
                                    {"status": "approved", "reviewed_at": datetime.now(timezone.utc).isoformat(), "reviewed_by": "human"}
                                ).eq("id", row["id"]).execute()
                                client.table("reviews_rejections").insert(
                                    {"proposal_id": row["id"], "reviewer_type": "human", "decision": "approve", "reason": "Approved via dashboard"}
                                ).execute()
                                fresh = client.table("proposals").select("*").eq("id", row["id"]).single().execute()
                                with st.spinner("Submitting order(s) to Alpaca..."):
                                    exec_result = execute_proposal(fresh.data)
                                if exec_result.success:
                                    st.success(exec_result.message)
                                else:
                                    st.error(exec_result.message)
                                refresh_all()
                        with btn_col2:
                            reject_reason = st.text_input("Rejection reason (optional)", key=f"reason_{row['id']}")
                            if st.button("Reject", key=f"reject_{row['id']}"):
                                client = get_client()
                                client.table("proposals").update(
                                    {"status": "rejected", "reviewed_at": datetime.now(timezone.utc).isoformat(), "reviewed_by": "human"}
                                ).eq("id", row["id"]).execute()
                                client.table("reviews_rejections").insert(
                                    {"proposal_id": row["id"], "reviewer_type": "human", "decision": "reject", "reason": reject_reason or "Rejected via dashboard"}
                                ).execute()
                                st.warning("Proposal rejected.")
                                refresh_all()

# ---------------------------------------------------------------------
# ALL PROPOSALS
# ---------------------------------------------------------------------
with tab_proposals:
    st.caption("Full proposal history across all statuses.")
    proposals_df = load_table("proposals", limit=300)
    if proposals_df.empty:
        st.info("No proposals yet.")
    else:
        status_filter = st.multiselect("Filter by status", options=sorted(proposals_df["status"].dropna().unique()), default=[])
        verdict_filter = st.multiselect("Filter by Critic verdict", options=sorted(proposals_df["critic_verdict"].dropna().unique()), default=[])
        filtered = proposals_df
        if status_filter:
            filtered = filtered[filtered["status"].isin(status_filter)]
        if verdict_filter:
            filtered = filtered[filtered["critic_verdict"].isin(verdict_filter)]
        display_cols = ["created_at", "symbol", "strategy_type", "timeframe", "status", "critic_verdict", "critic_risk_score", "proposer_confidence"]
        display_cols = [c for c in display_cols if c in filtered.columns]
        df_display = filtered[display_cols].copy()
        df_display.columns = ["Created", "Symbol", "Strategy", "Timeframe", "Status", "Verdict", "Risk Score", "Confidence"]
        st.dataframe(df_display, use_container_width=True, height=500)

# ---------------------------------------------------------------------
# TRADES
# ---------------------------------------------------------------------
with tab_trades:
    st.caption("Executed orders. realized_pnl populates once positions are closed.")
    trades_df = load_table("trades", limit=300)
    if trades_df.empty:
        st.info("No trades executed yet.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total trades", len(trades_df))
        filled = trades_df[trades_df["status"].isin(["filled", "closed", "submitted"])]
        m2.metric("Filled/closed", len(filled))
        if "realized_pnl" in trades_df.columns:
            total_pnl = trades_df["realized_pnl"].dropna().sum()
            m3.metric("Total realized P&L", f"${total_pnl:,.2f}")
        _, live_positions = get_account_snapshot()
        positions_by_symbol = {p.symbol: p for p in live_positions}
        open_trades = trades_df[trades_df["status"] == "submitted"]
        m4.metric("Open (unclosed)", len(open_trades))

        if not open_trades.empty:
            st.subheader("Open Positions — Live P&L")
            for _, row in open_trades.iterrows():
                pos = positions_by_symbol.get(row["symbol"])
                cols = st.columns([2, 1, 1, 1, 1, 1])
                cols[0].write(f"**{row['symbol']}**")
                cols[1].write(f"{row['side']} x{row['quantity']}")
                if pos is not None:
                    unrealized_pl = float(pos.unrealized_pl)
                    unrealized_pct = float(pos.unrealized_plpc) * 100
                    cols[2].write(f"${float(pos.market_value):,.2f}")
                    pnl_color = "🟢" if unrealized_pl >= 0 else "🔴"
                    cols[3].write(f"{pnl_color} ${unrealized_pl:,.2f}")
                    cols[4].write(f"{unrealized_pct:+.2f}%")
                else:
                    cols[2].write("—")
                    cols[3].write("no live position")
                    cols[4].write("—")
                if cols[5].button("Close", key=f"close_{row['id']}"):
                    st.session_state[f"confirm_close_{row['id']}"] = True
                if st.session_state.get(f"confirm_close_{row['id']}"):
                    st.warning(f"This will place a REAL paper order to close {row['symbol']}. Confirm?")
                    cc1, cc2 = st.columns(2)
                    if cc1.button("Yes, close it", key=f"confirm_yes_{row['id']}"):
                        result = close_position(row.to_dict())
                        if result.success:
                            st.success(result.message)
                        else:
                            st.error(result.message)
                        st.session_state[f"confirm_close_{row['id']}"] = False
                        load_table.clear()
                        st.rerun()
                    if cc2.button("Cancel", key=f"confirm_no_{row['id']}"):
                        st.session_state[f"confirm_close_{row['id']}"] = False
                        st.rerun()
            st.divider()

        display_cols = ["created_at", "symbol", "side", "quantity", "status", "fill_price", "realized_pnl", "alpaca_order_id"]
        display_cols = [c for c in display_cols if c in trades_df.columns]
        df_display = trades_df[display_cols].copy()
        df_display.columns = ["Created", "Symbol", "Side", "Qty", "Status", "Fill Price", "PnL", "Order ID"]
        st.dataframe(df_display, use_container_width=True, height=500)

# ---------------------------------------------------------------------
# BREAKER LOG
# ---------------------------------------------------------------------
with tab_breaker_log:
    st.caption("Full audit trail of every pre_scan / pre_execution / manual check.")
    breaker_df = load_table("circuit_breaker_events", limit=200)
    if breaker_df.empty:
        st.info("No circuit breaker events logged yet.")
    else:
        display_cols = ["created_at", "check_point", "tripped", "reasons", "equity", "daily_pnl_pct", "open_positions_count", "trades_today_count", "cleared_by_human"]
        display_cols = [c for c in display_cols if c in breaker_df.columns]
        df_display = breaker_df[display_cols].copy()
        df_display.columns = ["Time", "Check Point", "Tripped", "Reasons", "Equity", "Daily PnL %", "Open Positions", "Trades Today", "Cleared By Human"]
        st.dataframe(df_display, use_container_width=True, height=500)

# ---------------------------------------------------------------------
# DECISION JOURNAL
# ---------------------------------------------------------------------
with tab_journal:
    st.caption("Human-readable narrative of each trading decision.")
    journal_col1, journal_col2 = st.columns([3, 1])
    with journal_col1:
        journal_limit = st.slider("Number of recent decisions to show", 5, 50, 5)
    with journal_col2:
        st.write("")
        if st.button("Export full report (.md)"):
            report_md = build_markdown_report(limit=journal_limit)
            st.download_button("Download Decision Journal.md", data=report_md, file_name="decision_journal.md", mime="text/markdown")

    try:
        stories = get_recent_stories(limit=journal_limit)
    except Exception as e:
        stories = []
        st.warning(f"Could not load decision journal: {e}")

    if not stories:
        st.info("No proposals recorded yet — run a scan first.")
    else:
        for idx, s in enumerate(stories):
            proposal = s.get('proposal', {})
            verdict = proposal.get('critic_verdict', 'pending')
            symbol = proposal.get('symbol', '?')
            strategy = proposal.get('strategy_type', '?')
            created = proposal.get('created_at', '')[:16]

            if verdict.lower() == "approve":
                badge_class = "badge-approve"
                badge_text = "Approved"
            elif verdict.lower() == "flag":
                badge_class = "badge-flag"
                badge_text = "Flagged"
            elif verdict.lower() == "reject":
                badge_class = "badge-reject"
                badge_text = "Rejected"
            else:
                badge_class = "badge-pending"
                badge_text = "Pending"

            risk = proposal.get('critic_risk_score')
            risk_str = f"{risk:.2f}" if risk is not None else "—"
            risk_color = "#22C55E" if risk and risk < 0.3 else "#F59E0B" if risk and risk < 0.6 else "#EF4444"
            conf = proposal.get('proposer_confidence', 0)
            conf_pct = int(conf * 100) if conf else 0

            st.markdown(f'''
            <div style="background:#161B22; border:1px solid #30363D; border-radius:10px; padding:16px 20px; margin-bottom:14px; border-left:3px solid {risk_color};">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px;">
                    <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
                        <span style="font-size:16px; font-weight:600; color:#F0F6FC;">{symbol}</span>
                        <span style="color:#8B949E; font-size:13px;">{strategy}</span>
                        <span class="badge {badge_class}">{badge_text}</span>
                    </div>
                    <div style="display:flex; align-items:center; gap:16px; flex-wrap:wrap; font-size:13px;">
                        <span><span style="color:#8B949E;">Risk:</span> <span style="color:{risk_color}; font-weight:600;">{risk_str}</span></span>
                        <span><span style="color:#8B949E;">Confidence:</span> <span style="color:#F0F6FC;">{conf_pct}%</span></span>
                        <span style="color:#8B949E;">{created}</span>
                    </div>
                </div>
                <div style="color:#C9D1D9; font-size:13px; line-height:1.6; padding:12px 16px; background:#0D1117; border-radius:6px; margin-top:6px;">
                    {s.get("narrative", "No narrative available.").replace(chr(10), '<br>')}
                </div>
            </div>
            ''', unsafe_allow_html=True)

            legs = proposal.get('proposed_contracts')
            if legs:
                with st.expander("View legs"):
                    st.json(legs)

# ---------------------------------------------------------------------
# FOOTER
# ---------------------------------------------------------------------
st.divider()
col1, col2, col3 = st.columns([1, 2, 1])
with col2:
    st.markdown("""
    <div style="text-align: center; color: #8B949E; font-size: 13px; padding: 8px 0;">
        <span style="font-weight: 600; color: #A855F7;">UniTrader</span>
        <span style="color: #30363D; margin: 0 8px;">|</span>
        <span>Paper Trading</span>
        <span style="color: #30363D; margin: 0 8px;">|</span>
        <span>v1.0</span>
        <br>
        <span style="color: #484F58; font-size: 11px;">Built for Alpaca Hackathon 2026 - lablab.ai</span>
        <br>
        <span style="color: #8B949E; font-size: 12px; font-weight: 500;">Created by Aliff Thoha</span>
    </div>
    """, unsafe_allow_html=True)
