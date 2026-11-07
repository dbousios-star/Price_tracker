"""HTML fetching and price extraction (requirement 3.A).

``fetch_price(url, css_selector)`` returns a :class:`float` and raises a
descriptive :class:`ScraperError` subclass on any failure. Pages that hydrate
their price with client-side JavaScript can be handled by passing
``use_playwright=True`` (or ``"use_playwright": true`` in ``targets.json``).
"""
from __future__ import annotations

import logging
import math
import random
import re
import time
from typing import Optional, Sequence

import requests
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

#: Currency codes that may appear next to (or inside) the price text.
CURRENCY_CODE_RE = re.compile(
    r"\b(?:USD|EUR|GBP|JPY|CNY|RMB|INR|AUD|CAD|CHF|SEK|NOK|DKK|PLN|CZK|HUF|RON|"
    r"BRL|MXN|ZAR|TRY|RUB|KRW|SGD|HKD|NZD|ILS|AED|SAR|THB|IDR|MYR|PHP|VND)\b",
    re.IGNORECASE,
)

#: Everything that is not a digit, a decimal/thousands separator or a sign.
NON_PRICE_CHARS_RE = re.compile(r"[^0-9.,\-]")

_SPACE_CHARS = ("\u00a0", "\u202f", "\u2009", "\u2007", "\u200b")


class ScraperError(Exception):
    """Base class for every scraping failure."""


class HttpStatusError(ScraperError):
    """The server answered with a non-success HTTP status."""


class ElementNotFoundError(ScraperError):
    """The CSS selector did not match anything on the page."""


class PriceParseError(ScraperError):
    """The matched element did not contain a parsable price."""


def _normalize_separators(value: str) -> str:
    """Turn separator variants into a plain float literal.

    Handles ``1299.00`` / ``1,299.00`` / ``1.299,00`` / ``1 299,00`` /
    ``1.299.000``. When a single separator is followed by exactly three
    digits (``1,299``) it is treated as a thousands group, because retail
    prices do not use three decimal places.
    """
    has_comma = "," in value
    has_dot = "." in value

    if has_comma and has_dot:
        decimal_sep = "," if value.rfind(",") > value.rfind(".") else "."
        thousand_sep = "." if decimal_sep == "," else ","
        return value.replace(thousand_sep, "").replace(decimal_sep, ".")

    separator = "," if has_comma else "." if has_dot else ""
    if not separator:
        return value

    parts = value.split(separator)
    if len(parts) > 2:  # e.g. 1.299.000 -> thousands separators only
        return "".join(parts)

    integer_part, fraction_part = parts
    if len(fraction_part) == 3:  # e.g. 1,299 -> 1299
        return integer_part + fraction_part
    if not fraction_part:
        return integer_part
    return f"{integer_part}.{fraction_part}"


def parse_price_text(raw: str) -> float:
    """Convert a scraped price string into a ``float``.

    >>> parse_price_text("$1,299.00")
    1299.0
    >>> parse_price_text("1.299,00 €")
    1299.0
    >>> parse_price_text("USD 45")
    45.0
    """
    if raw is None:
        raise PriceParseError("Price text is None - the selector matched an empty element.")
    text = str(raw).strip()
    if not text:
        raise PriceParseError("Price text is empty - the selector matched an empty element.")

    cleaned = CURRENCY_CODE_RE.sub(" ", text)
    for space in _SPACE_CHARS:
        cleaned = cleaned.replace(space, " ")

    negative = False
    stripped = cleaned.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        negative = True
    if "-" in cleaned or "\u2212" in cleaned:
        negative = True

    cleaned = NON_PRICE_CHARS_RE.sub("", cleaned)
    if not cleaned:
        raise PriceParseError(f"No numeric value found in price text {text!r}.")

    normalized = _normalize_separators(cleaned)
    try:
        value = float(normalized)
    except ValueError as exc:
        raise PriceParseError(
            f"Could not parse a price from {text!r} (normalised to {normalized!r})."
        ) from exc

    if not math.isfinite(value):
        raise PriceParseError(f"Parsed price from {text!r} is not finite ({value!r}).")
    if negative and value > 0:
        value = -value
    if value < 0:
        raise PriceParseError(f"Refusing to use a negative price parsed from {text!r}.")
    return round(value, 4)


def _apply_price_divisor(
    value: float, price_divisor: float, css_selector: str, url: str
) -> float:
    """Undo shops that split a price across elements ("€79" + "00").

    The text join in :meth:`PriceScraper.extract_price` turns such a page into
    ``79.00`` -> ``7900``, so a target may declare ``"price_divisor": 100`` to
    divide the parsed number back down. See ``targets.json``.
    """
    try:
        divisor = float(price_divisor)
    except (TypeError, ValueError) as exc:
        raise ScraperError(
            f"price_divisor must be a number (got {price_divisor!r}) "
            f"[selector={css_selector!r}, url={url or 'n/a'}]"
        ) from exc
    if divisor <= 0:
        raise ScraperError(
            f"price_divisor must be greater than 0 (got {divisor!r}) "
            f"[selector={css_selector!r}, url={url or 'n/a'}]"
        )
    if divisor == 1.0:
        return value
    return round(value / divisor, 4)


class PriceScraper:
    """Fetches product pages and extracts a normalised price."""

    def __init__(
        self,
        timeout: float = 20.0,
        user_agents: Optional[Sequence[str]] = None,
        rotate_user_agents: bool = True,
        max_retries: int = 2,
        backoff_seconds: float = 1.5,
        session: Optional[requests.Session] = None,
        parser: str = "lxml",
    ) -> None:
        self.timeout = max(1.0, float(timeout))
        self.user_agents = tuple(user_agents or DEFAULT_USER_AGENTS)
        self.rotate_user_agents = rotate_user_agents
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.session = session or requests.Session()
        self.parser = parser
        self._fixed_user_agent = random.choice(self.user_agents)

    # -- HTTP --------------------------------------------------------------
    def _build_headers(self) -> dict[str, str]:
        user_agent = (
            random.choice(self.user_agents) if self.rotate_user_agents else self._fixed_user_agent
        )
        return {
            "User-Agent": user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Connection": "keep-alive",
        }

    def fetch_html(self, url: str) -> str:
        """GET ``url`` and return the decoded HTML, retrying transient errors."""
        if not url.lower().startswith(("http://", "https://")):
            raise ScraperError(f"Unsupported URL {url!r}: only http(s) URLs can be scraped.")

        attempts = self.max_retries + 1
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(
                    url, headers=self._build_headers(), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = ScraperError(f"GET {url} failed on attempt {attempt}/{attempts}: {exc}")
                LOGGER.warning("%s", last_error)
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response.text

                if status == 403:
                    message = (
                        f"GET {url} returned HTTP 403 (forbidden). The site is blocking "
                        "automated requests; try a different user agent or the Playwright fallback."
                    )
                elif status == 404:
                    message = f"GET {url} returned HTTP 404 (not found). Check the product URL."
                elif status == 429:
                    message = (
                        f"GET {url} returned HTTP 429 (rate limited). Slow the schedule down "
                        "or raise REQUEST_TIMEOUT."
                    )
                else:
                    message = f"GET {url} returned HTTP {status} {response.reason or ''}".strip()

                last_error = HttpStatusError(message)
                # 4xx (except 429) will not fix itself by retrying.
                if 400 <= status < 500 and status != 429:
                    raise last_error
                LOGGER.warning("%s", last_error)

            if attempt < attempts:
                time.sleep(self.backoff_seconds * attempt)

        raise last_error or ScraperError(f"Could not fetch {url}.")

    # -- parsing -----------------------------------------------------------
    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self.parser)
        except Exception as exc:  # unknown parser / lxml not installed
            if self.parser == "html.parser":
                raise ScraperError(f"Could not parse the HTML document: {exc}") from exc
            LOGGER.debug("Parser %r unavailable (%s); falling back to html.parser", self.parser, exc)
            return BeautifulSoup(html, "html.parser")

    def extract_price(
        self,
        html: str,
        css_selector: str,
        url: str = "",
        price_divisor: float = 1.0,
    ) -> float:
        """Return the float price of the first element matching ``css_selector``."""
        soup = self._make_soup(html)
        try:
            element = soup.select_one(css_selector)
        except Exception as exc:  # soupsieve raises on invalid selectors
            raise ScraperError(f"Invalid CSS selector {css_selector!r}: {exc}") from exc

        if element is None:
            raise ElementNotFoundError(
                f"No element matched selector {css_selector!r} on {url or 'the page'}. "
                "The selector may be stale, or the price may be rendered by JavaScript - "
                'set "use_playwright": true for this target.'
            )

        raw_text = element.get_text(" ", strip=True)
        try:
            value = parse_price_text(raw_text)
        except PriceParseError as exc:
            raise PriceParseError(
                f"{exc} [selector={css_selector!r}, url={url or 'n/a'}, raw_text={raw_text!r}]"
            ) from exc
        return _apply_price_divisor(value, price_divisor, css_selector, url)

    # -- public API --------------------------------------------------------
    def fetch_price(
        self,
        url: str,
        css_selector: str,
        use_playwright: bool = False,
        price_divisor: float = 1.0,
    ) -> float:
        """Fetch ``url`` and return the price found at ``css_selector``."""
        if use_playwright:
            return self._fetch_price_with_playwright(
                url, css_selector, price_divisor=price_divisor
            )
        html = self.fetch_html(url)
        return self.extract_price(
            html, css_selector, url=url, price_divisor=price_divisor
        )

    def _fetch_price_with_playwright(
        self, url: str, css_selector: str, price_divisor: float = 1.0
    ) -> float:
        """Headless-browser fallback for client-side rendered prices.

        Bot-protection interstitials (Cloudflare's "Just a moment...") beat
        plain HTTP clients entirely, and they do not always clear on the first
        page load, so the page is reloaded up to ``max_retries`` extra times
        while reusing the same browser context - any clearance cookie the first
        attempt earned is therefore kept for the retry.
        """
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ScraperError(
                "This target has use_playwright enabled but Playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            ) from exc

        timeout_ms = int(self.timeout * 1000)
        attempts = self.max_retries + 1
        last_error: Optional[Exception] = None

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(
                    user_agent=random.choice(self.user_agents), locale="el-GR"
                )
                for attempt in range(1, attempts + 1):
                    page = context.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        page.wait_for_selector(
                            css_selector, timeout=timeout_ms, state="attached"
                        )
                        raw_text = page.locator(css_selector).first.inner_text().strip()
                        return _apply_price_divisor(
                            parse_price_text(raw_text), price_divisor, css_selector, url
                        )
                    except PriceParseError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - playwright raises its own tree
                        last_error = ScraperError(
                            f"Playwright could not read {css_selector!r} from {url} "
                            f"(attempt {attempt}/{attempts}): {exc}"
                        )
                        LOGGER.warning("%s", last_error)
                    finally:
                        page.close()

                    if attempt < attempts:
                        time.sleep(self.backoff_seconds * attempt)

                raise ScraperError(
                    f"Playwright gave up on {url} after {attempts} attempt(s). "
                    "The price may be behind a bot-protection challenge: "
                    f"{last_error}"
                )
            finally:
                browser.close()


def fetch_price(
    url: str,
    css_selector: str,
    use_playwright: bool = False,
    timeout: float = 20.0,
    session: Optional[requests.Session] = None,
    price_divisor: float = 1.0,
) -> float:
    """Convenience one-shot wrapper around :class:`PriceScraper`."""
    scraper = PriceScraper(timeout=timeout, session=session)
    return scraper.fetch_price(
        url,
        css_selector,
        use_playwright=use_playwright,
        price_divisor=price_divisor,
    )


