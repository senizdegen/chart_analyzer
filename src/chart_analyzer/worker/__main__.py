"""Entry point: ``python -m chart_analyzer.worker``."""

import asyncio

from chart_analyzer.worker.runner import run

if __name__ == "__main__":
    asyncio.run(run())
