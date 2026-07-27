"""Standalone runner: `python scripts/setup_telegram.py`.

Thin wrapper around arb.tools.telegram_setup so you can run the Telegram setup
without going through the `arb` CLI. Requires the package to be installed
(`pip install -e .`) or src/ on PYTHONPATH.
"""

from __future__ import annotations

import asyncio

from arb.tools.telegram_setup import run

if __name__ == "__main__":
    asyncio.run(run())
