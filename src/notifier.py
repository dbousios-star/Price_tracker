"""Telegram and Discord alert formatting and dispatch (requirement 3.C).

Every alert is described once as an :class:`Alert` and rendered twice: as
Telegram HTML and as a Discord rich embed. Delivery failures are isolated per
channel so one broken webhook never blocks the other channel.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from .config import Settings, Target
from .database import PriceDelta

LOGGER = logging.getLogger(__name__)

TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"

COLOR_DROP = 0x2ECC71  # green
COLOR_INCREASE = 0xE74C3C  # red
COLOR_BASELINE = 0x95A5A6  # grey
COLOR_ERROR = 0xE67E22  # orange

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "RMB": "¥",
    "INR": "₹",
    "RUB": "₽",
    "TRY": "₺",
    "KRW": "₩",
    "ILS": "₪",
    "NGN": "₦",
    "PHP": "₱",
    "THB": "฿",
    "VND": "₫",
    "BRL": "R$",
    "AUD": "A$",
    "CAD": "C$",
    "NZD": "NZ$",
    "HKD": "HK$",
    "SGD": "S$",
    "MXN": "MX$",
    "ZAR": "R",
    "CHF": "CHF ",
    "SEK": "kr ",
    "NOK": "kr ",
    "DKK": "kr ",
    "PLN": "zł ",
    "CZK": "Kč ",
}

_TAG_RE = re.compile(r"<[^>]+>")


class NotificationError(Exception):
    """The channel accepted the request but rejected the payload."""


def format_money(value: Optional[float], currency: str = "") -> str:
    """``47.9`` + ``USD`` -> ``"$47.90"``; unknown currency -> ``"47.90 USD"``."""
    if value is None:
        return "n/a"
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper())
    if symbol is None:
        symbol = f"{(currency or '').upper()} " if currency else ""
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(float(value)):,.2f}"


def format_percent(percent: Optional[float]) -> str:
    """``-20.0`` -> ``"-20.00%"`` (always signed)."""
    if percent is None:
        return "n/a"
    return f"{percent:+.2f}%"


def _format_timestamp(moment: Optional[datetime] = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def strip_tags(text: str) -> str:
    """Convert Telegram HTML back into readable plain text for logs."""
    return html.unescape(_TAG_RE.sub("", text))


@dataclass(frozen=True)
class Alert:
    """A channel-agnostic notification."""

    title: str
    summary: str
    fields: tuple[tuple[str, str], ...] = ()
    color: int = COLOR_BASELINE
    url: str = ""
    emoji: str = ""
    footer: str = "Price Tracker"

    def telegram_text(self) -> str:
        lines = [f"{self.emoji} <b>{html.escape(self.title)}</b>".strip(), ""]
        for label, value in self.fields:
            lines.append(f"<b>{html.escape(label)}:</b> {html.escape(str(value))}")
        if self.url:
            lines.extend(["", f'<a href="{html.escape(self.url, quote=True)}">Open product page</a>'])
        return "\n".join(lines)

    def discord_embed(self) -> dict:
        embed: dict = {
            "title": f"{self.emoji} {self.title}".strip(),
            "color": self.color,
            "fields": [
                {"name": label, "value": str(value) or "-", "inline": True}
                for label, value in self.fields
            ],
            "footer": {"text": self.footer},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self.summary:
            embed["description"] = self.summary
        if self.url:
            embed["url"] = self.url
        return embed


def build_baseline_alert(target: Target, price: float) -> Alert:
    """First observation for a URL (requirement 3.C: "Tracking started")."""
    return Alert(
        title=f"Tracking started: {target.name}",
        summary=(
            f"Baseline recorded at {format_money(price, target.currency)}. "
            "You will be alerted when this price changes."
        ),
        emoji="📍",
        color=COLOR_BASELINE,
        url=target.url,
        footer="Price Tracker · baseline",
        fields=(
            ("Price", format_money(price, target.currency)),
            ("Recorded", _format_timestamp()),
        ),
    )


def build_change_alert(target: Target, price: float, delta: PriceDelta) -> Alert:
    """Price drop (high priority) or price increase notice."""
    dropped = delta.direction == "dropped"
    verb = "dropped" if dropped else "increased"
    magnitude = abs(delta.percent) if delta.percent is not None else None

    fields: list[tuple[str, str]] = [
        ("Old price", format_money(delta.previous, target.currency)),
        ("New price", format_money(price, target.currency)),
        ("Difference", format_money(delta.difference, target.currency)),
        (
            "Change",
            format_percent(delta.percent)
            if delta.percent is not None
            else format_money(delta.difference, target.currency),
        ),
    ]
    if dropped and delta.savings is not None:
        fields.append(("You save", format_money(delta.savings, target.currency)))
    fields.append(("Checked", _format_timestamp()))

    if magnitude is not None:
        summary = (
            f"{target.name} {verb} {magnitude:.2f}% from "
            f"{format_money(delta.previous, target.currency)} to "
            f"{format_money(price, target.currency)}."
        )
    else:
        summary = f"{target.name} changed price to {format_money(price, target.currency)}."

    return Alert(
        title=f"Price {verb}: {target.name}",
        summary=summary,
        emoji="🔻" if dropped else "🔺",
        color=COLOR_DROP if dropped else COLOR_INCREASE,
        url=target.url,
        footer="Price Tracker · " + ("price drop" if dropped else "price increase"),
        fields=tuple(fields),
    )


def build_error_alert(target: Target, error: str) -> Alert:
    """Scraping failure notice."""
    return Alert(
        title=f"Price check failed: {target.name}",
        summary="The price could not be read. The tracker will retry on the next scheduled run.",
        emoji="⚠️",
        color=COLOR_ERROR,
        url=target.url,
        footer="Price Tracker · error",
        fields=(("Error", str(error)[:500]), ("Failed at", _format_timestamp())),
    )


class Notifier:
    """Sends :class:`Alert` objects to every configured channel."""

    def __init__(
        self,
        settings: Settings,
        dry_run: bool = False,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.timeout = max(1.0, float(timeout))
        self._session = session or requests.Session()

    # -- channels ----------------------------------------------------------
    def _send_telegram(self, text: str) -> None:
        url = TELEGRAM_API_TEMPLATE.format(token=self.settings.telegram_bot_token)
        payload = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        response = self._session.post(url, json=payload, timeout=self.timeout)
        if response.status_code != 200:
            raise NotificationError(
                f"Telegram responded HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError:
            return
        if not body.get("ok", False):
            raise NotificationError(
                f"Telegram rejected the message: {body.get('description', 'unknown error')}"
            )

    def _send_discord(self, content: Optional[str], embeds: Optional[list[dict]]) -> None:
        payload: dict = {}
        if content:
            payload["content"] = content[:2000]
        if embeds:
            payload["embeds"] = embeds[:10]
        if not payload:
            raise NotificationError("Nothing to send: both content and embeds are empty.")
        response = self._session.post(
            self.settings.discord_webhook_url, json=payload, timeout=self.timeout
        )
        # Discord returns 204 No Content on success.
        if response.status_code not in (200, 204):
            raise NotificationError(
                f"Discord responded HTTP {response.status_code}: {response.text[:300]}"
            )

    def _attempt(self, channel: str, func, *args) -> str:
        try:
            func(*args)
        except Exception as exc:  # noqa: BLE001 - one channel must never break another
            LOGGER.warning("Failed to send alert via %s: %s", channel, exc)
            return f"failed: {exc}"
        LOGGER.info("Alert delivered via %s", channel)
        return "sent"

    # -- public API --------------------------------------------------------
    def dispatch(
        self,
        text: str,
        embeds: Optional[list[dict]] = None,
        content: Optional[str] = None,
    ) -> dict[str, str]:
        """Deliver a rendered alert; returns ``{channel: outcome}``."""
        if self.dry_run:
            LOGGER.info("[dry-run] Telegram payload:\n%s", strip_tags(text))
            LOGGER.info(
                "[dry-run] Discord payload:\n%s",
                json.dumps({"content": content, "embeds": embeds or []}, indent=2, ensure_ascii=False),
            )
            return {"dry-run": "logged"}

        results: dict[str, str] = {}
        if self.settings.telegram_enabled:
            results["telegram"] = self._attempt("telegram", self._send_telegram, text)
        if self.settings.discord_enabled:
            results["discord"] = self._attempt("discord", self._send_discord, content, embeds)

        if not results:
            LOGGER.info("[no channel configured] %s", strip_tags(text))
            results["none"] = "not configured"
        return results

    def send_alert(self, alert: Alert) -> dict[str, str]:
        return self.dispatch(
            alert.telegram_text(), [alert.discord_embed()], content=alert.summary
        )

    def notify_baseline(self, target: Target, price: float) -> dict[str, str]:
        return self.send_alert(build_baseline_alert(target, price))

    def notify_change(self, target: Target, price: float, delta: PriceDelta) -> dict[str, str]:
        return self.send_alert(build_change_alert(target, price, delta))

    def notify_error(self, target: Target, error: str) -> dict[str, str]:
        if not self.settings.notify_on_error:
            LOGGER.info("Error alerts are disabled (NOTIFY_ON_ERROR=false); not sending.")
            return {"disabled": "NOTIFY_ON_ERROR=false"}
        return self.send_alert(build_error_alert(target, error))


