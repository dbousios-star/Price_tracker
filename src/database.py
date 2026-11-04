"""SQLite persistence and price-delta calculations.

Schema (requirement 3.B)::

    price_history(
        id        INTEGER PRIMARY KEY,
        timestamp DATETIME,   -- stored as UTC ISO-8601 text (sortable)
        url       TEXT,
        price     REAL
    )
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    url       TEXT    NOT NULL,
    price     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_url_ts
    ON price_history (url, timestamp DESC);
"""

#: Changes smaller than half a cent are treated as "unchanged" so float noise
#: from currency parsing never produces a phantom alert.
CHANGE_EPSILON = 0.005


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string, e.g. ``2026-11-04T08:00:00+00:00``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str) -> datetime:
    """Parse a stored timestamp, assuming UTC when no offset is present."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class PricePoint:
    """One stored observation."""

    timestamp: datetime
    url: str
    price: float


@dataclass(frozen=True)
class PriceDelta:
    """Result of comparing the current price with the previous one."""

    previous: Optional[float]
    current: float
    difference: Optional[float]
    percent: Optional[float]

    @property
    def direction(self) -> str:
        """``baseline``, ``dropped``, ``increased`` or ``unchanged``."""
        if self.previous is None or self.difference is None:
            return "baseline"
        if self.difference < -CHANGE_EPSILON:
            return "dropped"
        if self.difference > CHANGE_EPSILON:
            return "increased"
        return "unchanged"

    @property
    def is_change(self) -> bool:
        return self.direction in {"dropped", "increased"}

    @property
    def savings(self) -> Optional[float]:
        """Positive amount saved, only when the price dropped."""
        if self.direction != "dropped" or self.difference is None:
            return None
        return abs(self.difference)

    def exceeds_threshold(self, threshold_pct: float) -> bool:
        """True when the change is large enough to warrant an alert."""
        if not self.is_change:
            return False
        if threshold_pct <= 0:
            return True
        if self.percent is None:  # previous price was 0 - compare absolutely
            return True
        return abs(self.percent) >= threshold_pct


def compute_change(current: float, previous: Optional[float]) -> PriceDelta:
    """Build a :class:`PriceDelta` for ``current`` versus ``previous``."""
    current = float(current)
    if previous is None:
        return PriceDelta(previous=None, current=current, difference=None, percent=None)

    previous = float(previous)
    difference = round(current - previous, 4)
    percent: Optional[float] = None
    if previous != 0:
        percent = round((difference / previous) * 100, 4)
    return PriceDelta(previous=previous, current=current, difference=difference, percent=percent)


class TrackerDB:
    """Thin, connection-per-call wrapper around ``tracker.db``."""

    def __init__(self, db_path: str | Path = "tracker.db") -> None:
        self.path = Path(db_path).expanduser()
        parent = self.path.parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"TrackerDB(path={str(self.path)!r})"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the database file and tables if they do not exist yet."""
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    # -- writes ------------------------------------------------------------
    def record_price(self, url: str, price: float, timestamp: Optional[str] = None) -> None:
        """Append a price observation for ``url``."""
        if not url or not str(url).strip():
            raise ValueError("url must be a non-empty string")
        value = float(price)
        if not math.isfinite(value):
            raise ValueError(f"price must be a finite number, got {price!r}")
        if value < 0:
            raise ValueError(f"price must be non-negative, got {price!r}")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO price_history (timestamp, url, price) VALUES (?, ?, ?)",
                (timestamp or utc_now_iso(), str(url).strip(), value),
            )

    # -- reads -------------------------------------------------------------
    def get_latest_price(self, url: str) -> Optional[float]:
        """Most recently recorded price for ``url`` (``None`` on first run)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT price FROM price_history WHERE url = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (str(url).strip(),),
            ).fetchone()
        return None if row is None else float(row["price"])

    def get_previous_price(self, url: str) -> Optional[float]:
        """The observation before the latest one (for post-write comparisons)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT price FROM price_history WHERE url = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1 OFFSET 1",
                (str(url).strip(),),
            ).fetchone()
        return None if row is None else float(row["price"])

    def get_history(self, url: str, limit: int = 30) -> list[PricePoint]:
        """Newest-first history for ``url``."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT timestamp, url, price FROM price_history WHERE url = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (str(url).strip(), max(1, int(limit))),
            ).fetchall()
        return [
            PricePoint(timestamp=parse_timestamp(row["timestamp"]), url=row["url"], price=float(row["price"]))
            for row in rows
        ]

    def get_price_stats(self, url: str) -> dict[str, Optional[float]]:
        """Aggregate statistics for ``url``."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, MIN(price) AS min_price, "
                "MAX(price) AS max_price, AVG(price) AS avg_price "
                "FROM price_history WHERE url = ?",
                (str(url).strip(),),
            ).fetchone()
        return {
            "count": int(row["count"] or 0),
            "min": None if row["min_price"] is None else float(row["min_price"]),
            "max": None if row["max_price"] is None else float(row["max_price"]),
            "avg": None if row["avg_price"] is None else round(float(row["avg_price"]), 4),
            "latest": self.get_latest_price(url),
        }

    def count_rows(self, url: Optional[str] = None) -> int:
        """Number of stored price points (optionally for a single URL)."""
        with self._connect() as connection:
            if url:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM price_history WHERE url = ?",
                    (str(url).strip(),),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS count FROM price_history").fetchone()
        return int(row["count"] or 0)
