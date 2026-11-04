"""Main runner: check prices, store history, send alerts (requirement 3.D).

Usage::

    python src/tracker.py                  # check every target
    python src/tracker.py --dry-run        # log alerts instead of sending them
    python src/tracker.py --send-now       # also post the current prices now
    python src/tracker.py --init-db        # create tracker.db and exit
    python src/tracker.py --target "Name"  # run a single target
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, Sequence

# Allow both `python src/tracker.py` and `python -m src.tracker`.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import (  # noqa: E402  (import after the sys.path fix)
    ConfigError,
    Settings,
    Target,
    load_settings,
    load_targets,
    resolve_path,
)
from src.database import PriceDelta, TrackerDB, compute_change  # noqa: E402
from src.notifier import Notifier, ReportEntry, format_money, format_percent  # noqa: E402
from src.scraper import PriceScraper, ScraperError  # noqa: E402

LOGGER = logging.getLogger("price_tracker")


@dataclass
class CheckResult:
    """Outcome of one target check."""

    target: Target
    status: str = "pending"
    price: Optional[float] = None
    previous: Optional[float] = None
    delta: Optional[PriceDelta] = None
    alerted: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status != "error"


def _truncate(text: str, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


class PriceTracker:
    """Coordinates scraping, persistence and alerting."""

    def __init__(
        self,
        settings: Settings,
        targets: Sequence[Target],
        scraper: Optional[PriceScraper] = None,
        database: Optional[TrackerDB] = None,
        notifier: Optional[Notifier] = None,
        dry_run: bool = False,
    ) -> None:
        self.settings = settings
        self.targets = list(targets)
        self.scraper = scraper or PriceScraper(timeout=settings.request_timeout)
        self.db = database or TrackerDB(settings.db_path)
        self.notifier = notifier or Notifier(settings, dry_run=dry_run)
        self.dry_run = dry_run

    # -- orchestration -----------------------------------------------------
    def run(self) -> list[CheckResult]:
        LOGGER.info("Checking %d target(s) | %s", len(self.targets), self.settings.describe())
        return [self.check_target(target) for target in self.targets]

    def check_target(self, target: Target) -> CheckResult:
        result = CheckResult(target=target)
        LOGGER.info("Checking %r", target.name)

        try:
            result.previous = self.db.get_latest_price(target.url)
            result.price = self.scraper.fetch_price(
                target.url, target.css_selector, use_playwright=target.use_playwright
            )
        except ScraperError as exc:
            return self._fail(result, exc)
        except Exception as exc:  # noqa: BLE001 - never let one target kill the run
            return self._fail(result, exc)

        result.delta = compute_change(result.price, result.previous)

        # Persist before notifying so a failed webhook never loses history.
        try:
            self.db.record_price(target.url, result.price)
        except Exception as exc:  # noqa: BLE001
            result.status = "error"
            result.error = f"database write failed: {exc}"
            LOGGER.error("[%s] %s", target.name, result.error)
            return result

        result.status = result.delta.direction
        LOGGER.info(
            "[%s] %s (previous %s -> current %s)",
            target.name,
            result.status,
            format_money(result.previous, target.currency),
            format_money(result.price, target.currency),
        )
        self._dispatch(result)
        return result

    def _fail(self, result: CheckResult, exc: Exception) -> CheckResult:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        LOGGER.error("[%s] %s", result.target.name, result.error)
        try:
            result.alerted = self.notifier.notify_error(result.target, result.error)
        except Exception as notify_error:  # noqa: BLE001
            LOGGER.warning("[%s] Could not send error alert: %s", result.target.name, notify_error)
        return result

    def _dispatch(self, result: CheckResult) -> None:
        """Decide which alert (if any) this result deserves (requirement 3.C)."""
        delta = result.delta
        if delta is None or result.price is None:
            return

        if delta.direction == "baseline":
            if self.settings.notify_on_baseline:
                result.alerted = self.notifier.notify_baseline(result.target, result.price)
            else:
                LOGGER.info(
                    "[%s] Baseline recorded; NOTIFY_ON_BASELINE=false so no alert was sent.",
                    result.target.name,
                )
            return

        if delta.direction == "unchanged":
            LOGGER.info(
                "[%s] Price unchanged at %s - no alert sent.",
                result.target.name,
                format_money(result.price, result.target.currency),
            )
            return

        if not delta.exceeds_threshold(self.settings.alert_threshold_pct):
            LOGGER.info(
                "[%s] Change of %s is below ALERT_THRESHOLD_PCT=%s - no alert sent.",
                result.target.name,
                format_percent(delta.percent),
                self.settings.alert_threshold_pct,
            )
            return

        if delta.direction == "increased" and not self.settings.notify_on_increase:
            LOGGER.info(
                "[%s] Price increased but NOTIFY_ON_INCREASE=false - no alert sent.",
                result.target.name,
            )
            return

        result.alerted = self.notifier.notify_change(result.target, result.price, delta)

    # -- reporting ---------------------------------------------------------
    def send_report(self, results: Sequence[CheckResult]) -> dict[str, str]:
        """Push the current prices, changed or not (``--send-now``).

        Change alerts stay quiet when nothing moved, so a manual or
        push-triggered run needs this to produce a message at all.
        """
        entries = [
            ReportEntry(result.target.name, result.price, result.target.currency)
            for result in results
            if result.price is not None
        ]
        if not entries:
            LOGGER.warning("No price to report: every target failed, snapshot not sent.")
            return {}

        failures = len(results) - len(entries)
        note = f"{failures} target(s) could not be checked." if failures else ""
        outcome = self.notifier.notify_report(entries, note)
        LOGGER.info("Price snapshot delivery: %s", outcome)
        return outcome

    def summary(self, results: Sequence[CheckResult]) -> str:
        """Human readable table for the end of the run."""
        header = ("Target", "Status", "Previous", "Current", "Change")
        rows: list[tuple[str, ...]] = [header]
        for result in results:
            currency = result.target.currency
            rows.append(
                (
                    _truncate(result.target.name, 36),
                    result.status if result.ok else "error",
                    format_money(result.previous, currency)
                    if result.previous is not None
                    else "-",
                    format_money(result.price, currency) if result.price is not None else "-",
                    format_percent(result.delta.percent)
                    if result.delta and result.delta.percent is not None
                    else "-",
                )
            )
        widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
        lines: list[str] = []
        for index, row in enumerate(rows):
            lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
            if index == 0:
                lines.append("  ".join("-" * width for width in widths))
        failures = [result for result in results if not result.ok]
        lines.append("")
        lines.append(
            f"{len(results) - len(failures)}/{len(results)} target(s) checked successfully"
            + (f", {len(failures)} failed" if failures else "")
            + (" (dry run: no alerts were sent)" if self.dry_run else "")
        )
        for result in failures:
            lines.append(f"  ! {result.target.name}: {result.error}")
        return "\n".join(lines)


EPILOG = """examples:
  python src/tracker.py                      check every target in targets.json
  python src/tracker.py --dry-run            print alerts instead of sending them
  python src/tracker.py --send-now           post the current prices right away
  python src/tracker.py --target "Chair"     run a single target by name
  python src/tracker.py --init-db            create tracker.db and exit
  python src/tracker.py --show-history URL   print the stored price history for URL
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker.py",
        description=(
            "Track product prices, store the history in SQLite and send "
            "Telegram/Discord alerts when a price moves."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--db", metavar="PATH", help="SQLite database path (default: tracker.db / $DB_PATH).")
    parser.add_argument("--targets", metavar="PATH", help="Path to targets.json (default: targets.json / $TARGETS_FILE).")
    parser.add_argument(
        "--target",
        metavar="NAME",
        action="append",
        dest="targets_filter",
        help="Only run the target(s) with this name. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch prices and update the database, but log notifications instead of sending them.",
    )
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="Also send a snapshot of the current prices, even when nothing changed.",
    )
    parser.add_argument("--init-db", action="store_true", help="Create the database schema and exit.")
    parser.add_argument("--list-targets", action="store_true", help="Print the configured targets and exit.")
    parser.add_argument("--show-history", metavar="URL", help="Print the stored price history for URL and exit.")
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default=None,
        help="DEBUG, INFO, WARNING or ERROR (default: INFO / $LOG_LEVEL).",
    )
    return parser


def configure_logging(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a TextIOWrapper
            pass
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        configure_logging("INFO")
        LOGGER.error("Configuration error: %s", exc)
        return 2

    configure_logging(args.log_level or settings.log_level)

    if args.db:
        settings = replace(settings, db_path=resolve_path(args.db, settings.db_path))

    if args.init_db:
        database = TrackerDB(settings.db_path)
        print(f"Database ready: {database.path} ({database.count_rows()} price point(s))")
        return 0

    if args.show_history:
        database = TrackerDB(settings.db_path)
        points = database.get_history(args.show_history, limit=50)
        if not points:
            print(f"No price history stored for {args.show_history}")
            return 0
        print(f"Price history for {args.show_history} (newest first, {len(points)} row(s)):")
        for point in points:
            stamp = point.timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
            print(f"  {stamp:26} {point.price:>12,.2f}")
        return 0

    try:
        targets = load_targets(args.targets)
    except ConfigError as exc:
        LOGGER.error("Target configuration error: %s", exc)
        return 2

    if args.targets_filter:
        wanted = {name.strip().lower() for name in args.targets_filter}
        selected = [target for target in targets if target.name.lower() in wanted]
        if not selected:
            available = ", ".join(target.name for target in targets) or "none"
            LOGGER.error("No target matched %s. Available targets: %s", args.targets_filter, available)
            return 2
        targets = tuple(selected)

    if args.list_targets:
        print(f"{len(targets)} configured target(s):")
        for target in targets:
            mode = "playwright" if target.use_playwright else "requests"
            currency = target.currency or "no currency"
            print(f"  - {target.name}\n      url: {target.url}")
            print(f"      selector: {target.css_selector} | renderer: {mode} | currency: {currency}")
        return 0

    for warning in settings.warnings():
        LOGGER.warning(warning)

    tracker = PriceTracker(settings, targets, dry_run=args.dry_run)
    results = tracker.run()
    print()
    print(tracker.summary(results))
    if args.send_now:
        tracker.send_report(results)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())


