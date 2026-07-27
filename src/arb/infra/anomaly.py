"""Anomaly detection & alert routing.

The live engine funnels errors and abnormal conditions here. Each anomaly is
logged and (best-effort) pushed to Telegram. This is the single choke point the
plan requires: "when any error or anomaly is detected, handle it AND notify".
"""

from __future__ import annotations

from enum import Enum

from arb.infra.logging import get_logger
from arb.infra.telegram import TelegramNotifier

log = get_logger(__name__)


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AnomalyReporter:
    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier

    async def report(self, severity: Severity, kind: str, detail: str) -> None:
        msg = f"[{severity.value}] {kind}: {detail}"
        if severity is Severity.CRITICAL:
            log.critical(msg)
        elif severity is Severity.WARNING:
            log.warning(msg)
        else:
            log.info(msg)
        # Info-level events stay in logs; escalate warnings/criticals to Telegram.
        if severity is not Severity.INFO:
            await self.notifier.send(f"⚠️ <b>{severity.value}</b> {kind}\n{detail}")
