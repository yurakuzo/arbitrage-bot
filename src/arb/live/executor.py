"""Execution router: turns a detected arbitrage into action per the mode.

  - paper      : record a simulated fill only (default; always safe).
  - semi_auto  : propose the trade via Telegram and place ONLY on explicit yes.
  - auto       : place both legs immediately.

Even in semi_auto/auto, every real order still goes through `provider.place_order`
which re-checks the `TradingGate`; if the gate is not fully open the executor
transparently falls back to paper. Real placement also translates the canonical
(event-aligned) outcome back to each venue's NATIVE yes/no side — getting this
wrong would place the opposite bet, so it is centralized here and unit-tested.
"""

from __future__ import annotations

from arb.config import ExecutionConfig
from arb.core.matching import CanonicalPair
from arb.core.pricing import ArbOpportunity
from arb.infra.anomaly import AnomalyReporter, Severity
from arb.infra.logging import get_logger
from arb.infra.telegram import TelegramNotifier
from arb.live.gate import TradingGate
from arb.live.simulator import PaperLedger
from arb.providers.base import Provider
from arb.providers.models import Order, Outcome, Side, TimeInForce

log = get_logger(__name__)


def _opposite(o: Outcome) -> Outcome:
    return Outcome.NO if o is Outcome.YES else Outcome.YES


def native_outcome(canonical_outcome: Outcome, event_outcome: Outcome) -> Outcome:
    """Map a canonical (YES==event) outcome back to the venue's native side.

    canonical YES == 'event happens' == the venue's `event_outcome` side;
    canonical NO == the opposite side.
    """
    return event_outcome if canonical_outcome is Outcome.YES else _opposite(event_outcome)


def build_orders(pair: CanonicalPair, opp: ArbOpportunity) -> list[Order]:
    orders: list[Order] = []
    for leg in opp.legs:
        v = leg.venue.value
        mapping = pair.legs[v]
        orders.append(
            Order(
                venue=leg.venue,
                market_id=mapping.market_id,
                outcome=native_outcome(leg.outcome, mapping.outcome),
                side=Side.BUY,
                contracts=leg.contracts,
                limit_price=leg.avg_price,
                tif=TimeInForce.FOK,
            )
        )
    return orders


def proposal_text(pair: CanonicalPair, opp: ArbOpportunity) -> str:
    lines = [f"🟢 Arbitrage: <b>{pair.canonical_id}</b>", pair.description or ""]
    for o in build_orders(pair, opp):
        lines.append(f"  BUY {o.contracts:.0f} {o.venue.value}:{o.outcome.value} @ ${o.limit_price:.3f}")
    lines.append(f"Net ${opp.net_profit:.2f}  ROI {opp.roi * 100:.2f}%")
    return "\n".join(x for x in lines if x)


class Executor:
    def __init__(
        self,
        mode: str,
        providers: dict[str, Provider],
        notifier: TelegramNotifier,
        ledger: PaperLedger,
        gate: TradingGate,
        execution: ExecutionConfig,
        anomaly: AnomalyReporter,
    ):
        self.mode = mode
        self.providers = providers
        self.notifier = notifier
        self.ledger = ledger
        self.gate = gate
        self.execution = execution
        self.anomaly = anomaly

    async def execute(self, pair: CanonicalPair, opp: ArbOpportunity, ts) -> bool:
        """Act on an opportunity. Returns True if a REAL order was placed."""
        # Paper mode, or gate not fully open -> simulate only.
        if self.mode == "paper" or not self.gate.places_real_orders:
            self.ledger.record(pair, opp, ts)
            return False

        if self.mode == "semi_auto":
            approved = await self.notifier.confirm(
                proposal_text(pair, opp), timeout_s=self.execution.confirm_timeout_s
            )
            if not approved:
                log.info("semi_auto: %s not approved / timed out — skipping", pair.canonical_id)
                return False

        return await self._place_legs(pair, opp, ts)

    async def _place_legs(self, pair: CanonicalPair, opp: ArbOpportunity, ts) -> bool:
        orders = build_orders(pair, opp)
        placed = []
        for order in orders:
            provider = self.providers.get(order.venue.value)
            if provider is None:
                await self.anomaly.report(
                    Severity.CRITICAL, "order_failed", f"no provider for {order.venue.value}"
                )
                return False
            try:
                result = await provider.place_order(order, self.gate)
            except Exception as exc:  # noqa: BLE001
                sev = Severity.CRITICAL if placed else Severity.WARNING
                await self.anomaly.report(
                    sev,
                    "order_failed",
                    f"{pair.canonical_id} {order.venue.value}:{order.outcome.value}: {exc}"
                    + (" — ONE LEG ALREADY FILLED, MANUAL REVIEW NEEDED" if placed else ""),
                )
                return False
            if not result.ok:
                sev = Severity.CRITICAL if placed else Severity.WARNING
                await self.anomaly.report(
                    sev, "order_rejected", f"{pair.canonical_id} {order.venue.value}: {result.status}"
                )
                return False
            placed.append(result)
        self.ledger.record(pair, opp, ts)
        await self.anomaly.report(
            Severity.INFO, "orders_placed", f"{pair.canonical_id}: {len(placed)} legs filled"
        )
        return True
