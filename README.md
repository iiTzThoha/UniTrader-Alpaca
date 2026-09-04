# UniTrader

**Autonomous AI Trading Agent with LLM Governance**

Built for the Alpaca AI Trading Agents Hackathon 2026 (lablab.ai)

---

## Overview

UniTrader is an autonomous AI trading agent that combines LLM-powered decision making with robust risk controls. It uses a Proposer-Critic architecture where one LLM generates trade ideas and another LLM evaluates them for risk, creating a built-in governance layer.

---

## Key Features

| Feature | Description |
|---------|-------------|
| Autonomous Trading | AI scans symbols and generates trade proposals without human intervention |
| LLM Governance | Proposer + Critic dual-model architecture for risk management |
| Circuit Breaker | Automatic safety limits on daily loss, positions, and trade count |
| Kill Switch | Manual halt for all trading activity with one click |
| Explainability Journal | Every decision is logged with full reasoning from both models |
| Live Charting | Real-time price charts with Today/Week/Month views |
| Review Queue | Human oversight with card-based proposal review |
| P&L Tracking | Live profit/loss monitoring for all positions |

---

## Tech Stack

| Category | Technology |
|----------|------------|
| Language | Python 3.11 |
| Dashboard | Streamlit |
| Trading API | Alpaca Trading API (Paper Trading) |
| Database | Supabase (PostgreSQL) |
| LLM Access | AI/ML API (OpenAI-compatible) |
| Market Data | yfinance + Alpaca |
| Charts | Plotly |
| Environment | WSL Ubuntu 20.04 |

---

┌─────────────────────────────────────────────────────────────┐
│ UniTrader │
├─────────────────────────────────────────────────────────────┤
│ │
│ Market Data ──► Proposer (LLM) ──► Critic (LLM) │
│ │ │ │
│ ▼ ▼ │
│ Trade Idea Risk Score │
│ │ │ │
│ └──────┬───────┘ │
│ ▼ │
│ Circuit Breaker │
│ │ │
│ ▼ │
│ Execute Order (Alpaca) │
│ │ │
│ ▼ │
│ Decision Journal (Supabase) │
│ │
└─────────────────────────────────────────────────────────────┘


---

## Dashboard Sections

| Tab | Purpose |
|-----|---------|
| Review Queue | Review and approve/reject AI-generated proposals with card-based UI |
| All Proposals | Full history of all proposals with filtering |
| Trades | Executed orders with live P&L tracking |
| Breaker Log | Audit trail of all circuit breaker events |
| Decision Journal | Explainable narratives of every trading decision |

---

## Safety Features

1. Circuit Breaker
   - Maximum daily loss (3%)
   - Maximum open positions (8)
   - Maximum trades per day
   - Sticky halt - requires manual reset

2. Kill Switch
   - One-click manual halt
   - Stops all scans and orders immediately
   - Resume with confirmation

3. Proposer-Critic Governance
   - Independent LLM review of every trade
   - Risk scoring (0-1 scale)
   - Rejection with rationale

---

## Project Structure

alpaca-hackathon/
├── dashboard/
│ └── app.py # Streamlit dashboard
├── agents/
│ ├── proposer.py # Trade idea generation
│ └── critic.py # Risk evaluation
├── core/
│ ├── circuit_breaker.py # Safety limits
│ ├── journal.py # Explainability logging
│ └── alpaca_client.py # Alpaca API wrapper
├── execution/
│ ├── executor.py # Order placement
│ └── closer.py # Position closing
├── persistence/
│ └── supabase_client.py # Database layer
├── signals/
│ ├── engine.py # Signal generation
│ └── universe.py # Watchlist management
└── config/
└── settings.py # Configuration


---

## Running Locally

```bash
# Clone the repository
git clone https://github.com/iiTzThoha/UniTrader-Alpaca.git
cd UniTrader-Alpaca

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your credentials

# Run the dashboard
streamlit run dashboard/app.py

Author
Aliff Thoha

GitHub: iiTzThoha

Hackathon
Event: Alpaca AI Trading Agents Hackathon 2026

Host: lablab.ai

Date: August 28 - September 4, 2026

Team: Solo

License
MIT License - see LICENSE file for details

Acknowledgments
Alpaca for the Trading API

AI/ML API for LLM access

lablab.ai for hosting the hackathon

Supabase for database hosting



## Architecture
