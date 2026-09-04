"""
Central configuration for the trading bot.

Everything else in the codebase should import `settings` from here rather
than reading os.environ directly. This keeps all tunables (thresholds,
mode, timeframe rules) in one auditable place, which matters a lot for
the "risk governance" story of this project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root regardless of where this module is imported from
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class ExecutionMode(str, Enum):
    MANUAL = "manual"   # proposals require human approval before execution
    AUTO = "auto"        # proposals execute automatically if they pass Critic + circuit breaker


class Timeframe(str, Enum):
    SHORT = "short"    # 0-2 weeks
    MEDIUM = "medium"  # 3-6 weeks
    LONG = "long"       # 2+ months


@dataclass(frozen=True)
class TimeframeRule:
    """Per-timeframe overrides. Longer horizons generally get looser
    circuit-breaker tolerance and different indicator weighting."""
    label: str
    min_days_to_expiry: int
    max_days_to_expiry: int | None  # None = no upper bound
    max_position_risk_pct: float    # % of account equity allowed per position


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Account-level circuit breaker thresholds. Checked pre-scan and
    pre-execution. All values are placeholders - tune during Day 4."""
    max_daily_loss_pct: float = 3.0          # halt if daily P&L drops below -X%
    max_daily_loss_absolute: float | None = None  # optional hard $ cap
    max_open_positions: int = 8
    max_single_trade_risk_pct: float = 2.0   # % of equity in any one trade
    max_trades_per_day: int = 15
    min_account_equity: float = 0.0          # safety floor, 0 disables
    cooldown_minutes_after_trip: int = 60    # sticky pause duration (informational;
                                              # actual reset is manual review by design)


@dataclass(frozen=True)
class Settings:
    # --- Alpaca ---
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    alpaca_base_url: str = field(
        default_factory=lambda: os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    )

    # --- Supabase ---
    supabase_url: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))
    supabase_publishable_key: str = field(
        default_factory=lambda: os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
    )
    supabase_secret_key: str = field(
        default_factory=lambda: os.getenv("SUPABASE_SECRET_KEY", "")
    )

    # --- LLM provider: AI/ML API (single key, OpenAI-compatible endpoint,
    # routes to many underlying models incl. Claude/GPT by model name) ---
    aiml_api_key: str = field(default_factory=lambda: os.getenv("AIML_API_KEY", ""))
    aiml_base_url: str = field(
        default_factory=lambda: os.getenv("AIML_BASE_URL", "https://api.aimlapi.com/v1")
    )
    proposer_model: str = field(default_factory=lambda: os.getenv("PROPOSER_MODEL", "gpt-4o"))
    critic_model: str = field(
        default_factory=lambda: os.getenv("CRITIC_MODEL", "anthropic/claude-sonnet-4.5")
    )

    # --- App behavior ---
    execution_mode: ExecutionMode = field(
        default_factory=lambda: ExecutionMode(os.getenv("EXECUTION_MODE", "manual"))
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # --- Active mode (swappable architecture) ---
    active_mode: str = "options_mode"  # or "equity_mode"

    # --- Timeframe rules (placeholders, tune on Day 2/4) ---
    timeframe_rules: dict[Timeframe, TimeframeRule] = field(default_factory=lambda: {
        Timeframe.SHORT: TimeframeRule("Short (0-2wk)", 0, 14, max_position_risk_pct=1.5),
        Timeframe.MEDIUM: TimeframeRule("Medium (3-6wk)", 15, 42, max_position_risk_pct=2.0),
        Timeframe.LONG: TimeframeRule("Long (2mo+)", 43, None, max_position_risk_pct=3.0),
    })

    # --- Circuit breaker ---
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    def validate(self) -> list[str]:
        """Returns a list of problems with the current config. Empty list = OK.
        Call this at startup so misconfiguration fails loudly, not silently."""
        problems = []
        if not self.alpaca_api_key:
            problems.append("ALPACA_API_KEY is not set")
        if not self.alpaca_secret_key:
            problems.append("ALPACA_SECRET_KEY is not set")
        if not self.supabase_url:
            problems.append("SUPABASE_URL is not set")
        if not self.supabase_secret_key:
            problems.append("SUPABASE_SECRET_KEY is not set")
        if "paper-api" not in self.alpaca_base_url:
            problems.append(
                f"ALPACA_BASE_URL does not look like a paper trading URL: {self.alpaca_base_url} "
                "(safety check - this project should never point at live trading)"
            )
        return problems


settings = Settings()


if __name__ == "__main__":
    # Quick manual check: `python -m config.settings`
    problems = settings.validate()
    if problems:
        print("Configuration problems found:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("Configuration OK.")
        print(f"Execution mode: {settings.execution_mode.value}")
        print(f"Active mode: {settings.active_mode}")
