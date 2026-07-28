"""The trading gate — the single authority on whether a REAL order may be placed.

Live execution is dangerous, so it is guarded by multiple independent switches
that must ALL be affirmative. Anything less than a full, deliberate opt-in resolves
to "paper" and no real order can leave the process:

  1. `ARB_LIVE_TRADING=true`      — master switch in the environment (default off)
  2. execution.mode in {semi_auto, auto}   — paper mode never places
  3. ARB_ENVIRONMENT == "prod"    — demo/sandbox never places real orders
  4. per-order stake <= execution.max_order_stake_usd  — hard capital cap

`check_order()` raises `TradingBlocked` unless every condition holds. Every
provider `place_order` implementation calls this first — there is no code path to
a real order that bypasses the gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from arb.config import ExecutionConfig, Settings
from arb.infra.logging import get_logger

log = get_logger(__name__)


class TradingBlocked(RuntimeError):
    """Raised when a real order is attempted but the gate is not fully open."""


@dataclass(frozen=True, slots=True)
class TradingGate:
    live_trading: bool
    mode: str
    environment: str
    max_order_stake_usd: float

    @classmethod
    def from_config(cls, settings: Settings, execution: ExecutionConfig) -> TradingGate:
        return cls(
            live_trading=settings.live_trading,
            mode=execution.mode,
            environment=settings.environment,
            max_order_stake_usd=execution.max_order_stake_usd,
        )

    @property
    def places_real_orders(self) -> bool:
        """True only when the full opt-in is present. If any check fails, we are
        in effect paper-only regardless of the requested mode."""
        return (
            self.live_trading
            and self.mode in ("semi_auto", "auto")
            and self.environment == "prod"
        )

    def check_order(self, stake_usd: float) -> None:
        """Raise TradingBlocked unless a real order of this size is permitted."""
        if not self.live_trading:
            raise TradingBlocked("live trading disabled (ARB_LIVE_TRADING not true)")
        if self.mode not in ("semi_auto", "auto"):
            raise TradingBlocked(f"execution mode '{self.mode}' does not place real orders")
        if self.environment != "prod":
            raise TradingBlocked(f"environment '{self.environment}' is not prod")
        if stake_usd > self.max_order_stake_usd:
            raise TradingBlocked(
                f"order stake ${stake_usd:.2f} exceeds cap ${self.max_order_stake_usd:.2f}"
            )

    def describe(self) -> str:
        state = "LIVE (places real orders)" if self.places_real_orders else "paper (safe)"
        return (
            f"{state} | live_trading={self.live_trading} mode={self.mode} "
            f"env={self.environment} max_order=${self.max_order_stake_usd:.2f}"
        )
