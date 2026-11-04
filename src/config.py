"""Configuration loading.

Reads ``.env`` through python-dotenv, exposes strongly typed settings and
loads the tracked targets from ``targets.json`` (or the ``TARGETS_JSON``
environment variable / a built-in demo list).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "tracker.db"
DEFAULT_TARGETS_FILE = PROJECT_ROOT / "targets.json"

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


class ConfigError(Exception):
    """Raised when configuration values are missing or malformed."""


def resolve_path(raw: str | os.PathLike[str] | None, default: Path) -> Path:
    """Resolve ``raw`` against the project root, falling back to ``default``."""
    if raw is None or not str(raw).strip():
        return default
    candidate = Path(str(raw).strip()).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    raise ConfigError(f"{name}={raw!r} is not a valid boolean (use true/false).")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid number.") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from the environment."""

    db_path: Path = DEFAULT_DB_PATH
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    alert_threshold_pct: float = 0.0
    notify_on_increase: bool = True
    notify_on_baseline: bool = True
    notify_on_error: bool = True
    request_timeout: float = 20.0
    log_level: str = "INFO"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def enabled_channels(self) -> tuple[str, ...]:
        channels = []
        if self.telegram_enabled:
            channels.append("telegram")
        if self.discord_enabled:
            channels.append("discord")
        return tuple(channels)

    def warnings(self) -> tuple[str, ...]:
        """Non-fatal configuration problems worth surfacing in the log."""
        messages: list[str] = []
        if not self.telegram_enabled and (self.telegram_bot_token or self.telegram_chat_id):
            messages.append(
                "Telegram is partially configured: both TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID are required. Channel disabled."
            )
        if not self.telegram_enabled and not self.discord_enabled:
            messages.append(
                "No notification channels configured - alerts will only be logged. "
                "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL."
            )
        if self.alert_threshold_pct < 0:
            messages.append(
                f"ALERT_THRESHOLD_PCT={self.alert_threshold_pct} is negative; "
                "treating it as 0 (alert on any change)."
            )
        return tuple(messages)

    def describe(self) -> str:
        channels = ", ".join(self.enabled_channels) or "none (log only)"
        return (
            f"db={self.db_path} | channels={channels} | "
            f"threshold={self.alert_threshold_pct}% | timeout={self.request_timeout}s"
        )


@dataclass(frozen=True)
class Target:
    """A single product to track."""

    name: str
    url: str
    css_selector: str
    currency: str = ""
    use_playwright: bool = False

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ConfigError("Every target needs a non-empty 'name'.")
        if not str(self.url).lower().startswith(("http://", "https://")):
            raise ConfigError(
                f"Target {self.name!r}: 'url' must start with http:// or https:// "
                f"(got {self.url!r})."
            )
        if not str(self.css_selector).strip():
            raise ConfigError(
                f"Target {self.name!r}: 'css_selector' must be a non-empty CSS selector."
            )


#: Used when neither targets.json nor TARGETS_JSON is available, so a fresh
#: clone can run end-to-end immediately against a public scraping sandbox.
DEFAULT_TARGETS: tuple[Target, ...] = (
    Target(
        name="A Light in the Attic (books.toscrape demo)",
        url="https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
        css_selector="p.price_color",
        currency="GBP",
    ),
)


def load_settings(env_file: Optional[Path] = None) -> Settings:
    """Load ``.env`` (if present) and build a :class:`Settings` instance."""
    env_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        # No .env (normal in CI): pick up real environment variables only.
        load_dotenv(override=False)

    log_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        log_level = "INFO"

    settings = Settings(
        db_path=resolve_path(os.getenv("DB_PATH"), DEFAULT_DB_PATH),
        telegram_bot_token=(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None,
        telegram_chat_id=(os.getenv("TELEGRAM_CHAT_ID") or "").strip() or None,
        discord_webhook_url=(os.getenv("DISCORD_WEBHOOK_URL") or "").strip() or None,
        alert_threshold_pct=_env_float("ALERT_THRESHOLD_PCT", 0.0),
        notify_on_increase=_env_bool("NOTIFY_ON_INCREASE", True),
        notify_on_baseline=_env_bool("NOTIFY_ON_BASELINE", True),
        notify_on_error=_env_bool("NOTIFY_ON_ERROR", True),
        request_timeout=_env_float("REQUEST_TIMEOUT", 20.0),
        log_level=log_level,
    )
    if settings.request_timeout <= 0:
        raise ConfigError("REQUEST_TIMEOUT must be greater than 0 seconds.")
    return settings


def _target_from_mapping(index: int, data: Mapping[str, Any]) -> Target:
    if not isinstance(data, Mapping):
        raise ConfigError(f"Target #{index + 1} must be a JSON object, got {type(data).__name__}.")
    missing = [key for key in ("url", "css_selector") if not str(data.get(key) or "").strip()]
    if missing:
        raise ConfigError(f"Target #{index + 1} is missing required key(s): {', '.join(missing)}.")
    name = str(data.get("name") or f"target-{index + 1}").strip()
    return Target(
        name=name,
        url=str(data["url"]).strip(),
        css_selector=str(data["css_selector"]).strip(),
        currency=str(data.get("currency") or "").strip().upper(),
        use_playwright=bool(data.get("use_playwright", False)),
    )


def _parse_targets_payload(payload: Any, source: str) -> tuple[Target, ...]:
    if isinstance(payload, Mapping):
        payload = payload.get("targets")
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise ConfigError(f"{source}: expected a JSON array of targets.")
    targets = tuple(_target_from_mapping(i, item) for i, item in enumerate(payload))
    if not targets:
        raise ConfigError(f"{source}: the targets list is empty.")
    seen: set[str] = set()
    for target in targets:
        key = target.url + "|" + target.css_selector
        if key in seen:
            raise ConfigError(f"{source}: duplicate target for {target.url} with selector {target.css_selector!r}.")
        seen.add(key)
    return targets


def load_targets(path: str | os.PathLike[str] | None = None) -> tuple[Target, ...]:
    """Load targets from an explicit path, ``TARGETS_FILE``, ``TARGETS_JSON`` or defaults."""
    candidates: list[Path] = []
    if path:
        candidates.append(resolve_path(path, DEFAULT_TARGETS_FILE))
    env_path = os.getenv("TARGETS_FILE")
    if env_path:
        candidates.append(resolve_path(env_path, DEFAULT_TARGETS_FILE))
    candidates.append(DEFAULT_TARGETS_FILE)

    for candidate in candidates:
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{candidate}: invalid JSON - {exc}") from exc
            return _parse_targets_payload(payload, str(candidate))

    raw_json = os.getenv("TARGETS_JSON")
    if raw_json and raw_json.strip():
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"TARGETS_JSON is not valid JSON - {exc}") from exc
        return _parse_targets_payload(payload, "TARGETS_JSON")

    return DEFAULT_TARGETS

