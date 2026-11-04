# Automated Product Price Tracker & Alert System

A small, modular Python tool that checks product prices on a schedule, keeps a
local price history in SQLite, and sends rich alerts to **Telegram** and
**Discord** when a price drops or rises.

- **Scrape** a product page with `requests` + `beautifulsoup4` (realistic
  desktop browser headers, retries, descriptive errors).
- **Fall back to Playwright** for pages that render prices with client-side
  JavaScript — one flag per target, no refactoring.
- **Store** every observation in `tracker.db` (`sqlite3`, standard library).
- **Calculate** absolute difference, percentage change, and savings.
- **Notify** via the Telegram Bot API and/or a Discord webhook, with silent
  logging when nothing changed.
- **Run itself** from GitHub Actions every day at 08:00 UTC and commit the
  database back so history persists between runs.

---

## 1. Project structure

```
price_tracker/
├── .env.example                 # copy to .env and fill in
├── .gitignore
├── requirements.txt
├── README.md
├── targets.json                 # the products you want to track
├── src/
│   ├── __init__.py
│   ├── config.py                # env vars, settings, target loading
│   ├── database.py              # SQLite schema, reads/writes, delta maths
│   ├── scraper.py               # fetch + extract + normalise price
│   ├── notifier.py              # Telegram / Discord formatting & dispatch
│   └── tracker.py               # CLI runner (entry point)
└── .github/
    └── workflows/
        └── daily_check.yml      # daily cron + manual trigger
```

Data flow: `tracker.py` → `scraper.fetch_price()` → `database.record_price()`
→ `database.compute_change()` → `notifier.notify_*()`.

---

## 2. Prerequisites

- Python 3.10 or newer (developed and tested on 3.12)
- pip
- Optional: a Telegram bot token and/or a Discord webhook URL
- Optional: Playwright (only for JavaScript-rendered pages)

---

## 3. Quick start

```powershell
# 1. enter the project
cd price_tracker

# 2. create an isolated environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate         # macOS / Linux

# 3. install dependencies
pip install -r requirements.txt

# 4. create your configuration
Copy-Item .env.example .env        # Windows
# cp .env.example .env             # macOS / Linux
# ...then edit .env and add your tokens

# 5. create the database and inspect the resolved config
python src/tracker.py --init-db
python src/tracker.py --list-targets

# 6. safe first run: alerts are printed, not sent
python src/tracker.py --dry-run

# 7. real run
python src/tracker.py
```

The database file `tracker.db` is created in the project root (override with
`DB_PATH` or `--db`). `targets.json` ships with two sandbox targets on
<https://books.toscrape.com> so the first run succeeds without editing
anything.

---

## 4. Configuration

### 4.1 `.env`

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | – | Chat/user/channel id that receives alerts |
| `DISCORD_WEBHOOK_URL` | – | Webhook URL to post embeds to |
| `DB_PATH` | `tracker.db` | SQLite file (relative paths resolve to the project root) |
| `TARGETS_FILE` | `targets.json` | Target list to use |
| `ALERT_THRESHOLD_PCT` | `0` | Minimum absolute % change before alerting (`0` = any change, `5` = only 5%+) |
| `NOTIFY_ON_INCREASE` | `true` | Send a notice when a price goes up |
| `NOTIFY_ON_BASELINE` | `true` | Send a "tracking started" message on the first run |
| `NOTIFY_ON_ERROR` | `true` | Send an alert when a page cannot be scraped |
| `REQUEST_TIMEOUT` | `20` | HTTP timeout in seconds |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Both channels are optional: with none configured the tool still tracks prices
and logs the alert text. Missing/partial credentials only produce warnings —
they never abort a run.

### 4.2 `targets.json`

A JSON array (a top-level `{"targets": [...]}` object is also accepted):

| Key | Required | Description |
|---|---|---|
| `name` | recommended | Label used in alerts and logs (defaults to `target-N`) |
| `url` | **yes** | Full product URL (`http://` or `https://`) |
| `css_selector` | **yes** | CSS selector of the element that contains the price |
| `currency` | no | ISO code (`USD`, `EUR`, `GBP`, …) used to format amounts |
| `use_playwright` | no | `true` renders the page in headless Chromium |

```json
[
  {
    "name": "Sony WH-1000XM5",
    "url": "https://example.com/products/wh-1000xm5",
    "css_selector": "span[data-price]",
    "currency": "USD",
    "use_playwright": false
  }
]
```

**Finding a selector:** open the product page, right-click the price →

*Inspect*, and copy the element's class/id (for example `p.price_color`,
`.product-price .value`, `#priceblock_ourprice`). Prefer stable attributes over
positional chains.

Targets may also be supplied through the `TARGETS_JSON` environment variable
(useful for CI-only overrides); the precedence is `--targets` → `TARGETS_FILE`
→ `targets.json` → `TARGETS_JSON` → built-in demo target.

### 4.3 Telegram setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Send your new bot a message (e.g. `hello`) so a chat exists.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `result[0].message.chat.id` (group ids are negative, e.g. `-1001234567890`).
4. Put both values in `.env`.

### 4.4 Discord setup

1. Server **Settings → Integrations → Webhooks → New Webhook**.
2. Pick the channel, **Copy Webhook URL**.
3. Set `DISCORD_WEBHOOK_URL` in `.env`.

Alerts are sent as a colored embed (green = drop, red = increase, grey =
baseline, orange = error) with old/new price, difference, percentage and a
link to the product.

---

## 5. Command line reference

```text
python src/tracker.py                      # check every target
python src/tracker.py --dry-run            # fetch + store, print alerts instead of sending
python src/tracker.py --target "Chair"     # only the named target (repeatable)
python src/tracker.py --init-db            # create tracker.db and exit
python src/tracker.py --list-targets       # show resolved targets
python src/tracker.py --show-history URL   # print stored price points for URL
python src/tracker.py --db other.db        # use a different database file
python src/tracker.py --targets my.json    # use a different target file
python src/tracker.py --log-level DEBUG    # verbose logging
```

Exit codes: `0` = every target checked, `1` = at least one target failed,
`2` = configuration error. The summary table is printed at the end:

```text
Target                                Status     Previous  Current  Change
------------------------------------  ---------  --------  -------  --------
A Light in the Attic (books.toscrape) baseline   -         £51.77   -

1/1 target(s) checked successfully
```

---

## 6. Currency normalisation

`parse_price_text()` strips symbols (`$ € £ ¥ ₹ ₽ ₺ ₩ ₪ ₦ ₱ ฿ ₫`), currency
codes (`USD`, `EUR`, `GBP`, …), spaces, non-breaking spaces and letters, then
decides which separator is decimal:

| Raw text | Parsed |
|---|---|
| `$1,299.00` | `1299.0` |
| `1.299,00 €` | `1299.0` |
| `USD 45` | `45.0` |
| `£ 1 299,99` | `1299.99` |
| `1.299.000` | `1299000.0` |
| `(12.50)` | raises `PriceParseError` (negative) |
| `Call for price` | raises `PriceParseError` |

Rule of thumb: when a single separator is followed by exactly three digits
(`1,299`) it is treated as a thousands group, because retail prices do not use
three decimal places. Prices are refused rather than guessed when they are
negative, empty or non-numeric.

---

## 7. Scheduling with GitHub Actions

`.github/workflows/daily_check.yml` runs on:

- `schedule: - cron: "0 8 * * *"` → daily at 08:00 UTC
- `workflow_dispatch` → manual run, with an optional **dry run** checkbox
- `push` that touches the workflow file (useful to smoke-test a change)

### Setup

1. Push the project to a GitHub repository.
2. **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `DISCORD_WEBHOOK_URL`
   (skip any channel you do not use)
3. **Settings → Actions → General → Workflow permissions** → select
   **Read and write permissions** so the job can commit `tracker.db`.
4. Run the workflow once manually (**Actions → Daily Price Check → Run
   workflow**) and check the log.
5. Note: scheduled workflows are disabled automatically after 60 days without
   repository activity; re-enable from the Actions tab if that happens.

The job writes the new rows to `tracker.db` and commits the file back to the
current branch with a `[skip ci]` marker, so history accumulates between runs.

> **Privacy note:** committing the database means your price history is public
> if the repository is public. Use a private repo, or drop the "Commit updated
> price history" step and instead persist `tracker.db` as an artifact / on a
> dedicated `data` branch.

---

## 8. JavaScript-rendered prices (Playwright)

If the price is injected after page load, `requests` will see an empty element.
Then:

```powershell
pip install playwright
playwright install chromium
```

and set `"use_playwright": true` for that target in `targets.json`. The scraper
launches headless Chromium, waits for the selector and reads the rendered text.
Nothing else changes — the database, delta maths and notifier are identical.
In GitHub Actions also add a step before the run:

```yaml
      - name: Install Playwright browser
        run: |
          pip install playwright
          playwright install --with-deps chromium
```

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ElementNotFoundError` | Selector is stale, or the price is rendered by JS → update `css_selector`, or set `use_playwright` |
| `PriceParseError` | The element contains text like "See price in cart"; pick a numeric element or a `content` attribute selector |
| `HTTP 403` | Bot protection: the tool already sends desktop headers — try Playwright, or slow the schedule |
| `HTTP 429` | Rate limited: lower the frequency, raise `REQUEST_TIMEOUT` |
| No alerts arrive, but runs say "sent" | Check the bot has been messaged once and the chat id is correct (`getUpdates`) |
| `Telegram responded HTTP 400` | Usually a wrong `chat_id` or a malformed token |
| Nothing happens on a price change | `ALERT_THRESHOLD_PCT` too high, or `NOTIFY_ON_INCREASE=false` |
| Baseline message on every run | The URL string differs between runs (tracking params, trailing slash) — normalise it in `targets.json` |
| History empty after CI run | `tracker.db` was not committed — check workflow permissions and the commit step |
| Prices look 1000× too large | The site uses `.` as a thousands separator without decimals (e.g. `1.299`) |

Debug with `--dry-run --log-level DEBUG`.

---

## 10. Notes and limitations

- Scraping must respect the target site's Terms of Service and `robots.txt`.
  Keep the schedule modest (daily is deliberately conservative).
- A CSS selector is required per target: automatic price detection is
  intentionally not attempted, because it is unreliable and hard to debug.
- Prices are stored as `REAL` and timestamps as UTC ISO-8601 text; timestamps
  are second-precision, so two runs inside the same second are ordered by the
  auto-incrementing `id`.
- The tool stores one row per check, per target; there is no retention pruning.
  For long-running trackers consider periodic `DELETE FROM price_history WHERE
  timestamp < ...` or exporting to CSV.
- Playwright is optional and not listed in `requirements.txt` by default to keep
  CI fast; enable it only when a target needs it.


