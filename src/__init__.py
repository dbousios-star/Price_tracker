"""Automated product price tracker and alert system.

Modules
-------
config    Environment + target configuration.
database  SQLite price history and delta calculations.
scraper   HTML fetching, price extraction and currency normalisation.
notifier  Telegram / Discord alert formatting and dispatch.
tracker   CLI runner that wires everything together.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
