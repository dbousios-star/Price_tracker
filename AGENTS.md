# AGENTS.md - rules for anyone (human or AI) working on price_tracker

Read this before changing anything. It records the things that have already
broken once, so they do not break twice.

## 1. What this project is, and its one job

A small Python price tracker for two Skroutz products. It scrapes a price with
Playwright, appends it to `tracker.db` (SQLite, committed to this repo), and
sends a Discord message when something is worth knowing. GitHub Actions runs it
six times a day (08:23-18:23 UTC) so it keeps working with the PC off, and
commits the updated database back with `[skip ci]`.

**The core goal:** reliable, unattended price history with alerts that matter.
A change that makes it fail silently, lose history, or turn into six
notifications a day is a regression - flag it before doing it, even if asked.

## 2. Two writers, one database - read this first

`tracker.db` is a single SQLite file written by two independent processes:

- the GitHub Actions cron (commits its copy back to `main`), and
- any local run (`run_tracker.cmd`, `Send Prices Now.cmd`, `python src/tracker.py`).

Both extend the same `id` sequence from whatever base they started on, so two
runs that are not in sync produce a **fork that cannot be merged**. Rules:

1. Before any local run that writes the real database: `git pull --ff-only`.
2. After it, push or discard (`git add -f tracker.db`, commit, `git push`).
3. If you do not intend to push, do not write the tracked file. Copy it first
   and run with `DB_PATH` (or the `--db` flag) pointing at the copy - see section 3.
4. If local and remote diverged, **the bot's copy is canonical**:
   `git checkout -- tracker.db` then `git pull --ff-only`. Never merge rows.

## 3. Inspecting the database safely

Never query `tracker.db` in place. Read the committed blob into `%TEMP%`, query
the copy, delete it:

```
python -c "import subprocess,os,sqlite3; p=os.path.join(os.environ['TEMP'],'x.db'); open(p,'wb').write(subprocess.run(['git','cat-file','blob','origin/main:tracker.db'],capture_output=True).stdout); c=sqlite3.connect(p); print(c.execute('select count(*) from price_history').fetchone())"
```

Schema: `price_history(id INTEGER PRIMARY KEY, timestamp TEXT ISO-8601 UTC,
url TEXT, price REAL)`.

## 4. A green run proves nothing; the commit does

The "Commit updated price history" step ends with

```
if git diff --cached --quiet; then echo "::error::No new price rows were written"; exit 1; fi
```

so a bot commit can only exist if the run inserted rows and pushed them. Never
remove or soften that guard. If "commit only when the price changed" is ever
wanted, the guard must be rewritten in the *same* commit, or every quiet run
turns red.

Proof of a real cloud run:

```powershell
git fetch origin
git log --oneline origin/main -3   # expect "chore: update price history [skip ci]"
git status -sb                     # expect "## main...origin/main", no ahead/behind
```

## 5. The workflow's constraints

- **The cron minute must not be `0`.** GitHub documents start-of-hour congestion
  where scheduled jobs are delayed and, under load, dropped. Current schedule:
  `23 8,10,12,14,16,18 * * *`.
- The shortest interval GitHub allows is 5 minutes.
- Cron is **UTC**; the user is in Athens (UTC+2 in winter, UTC+3 in summer).
- Private repo: 2,000 free Action minutes per month. Six runs a day is roughly
  400-700 minutes. **Keep the "Cache Playwright browsers" step** - it saves
  about a minute per run.
- Scheduled workflows only fire from the default branch (`main`).
- The `push` trigger watches only `.github/workflows/daily_check.yml` and
  `test_push.md`, and runs with `--send-now`, so a snapshot is always sent.

## 6. Alerting is drops-only, on purpose

`NOTIFY_ON_INCREASE=false` and `ALERT_THRESHOLD_PCT=0` live in the workflow's
`env`. The order in `_dispatch` is: baseline -> unchanged -> threshold ->
increase suppression -> send. A rise is stored in history and stays silent;
falls and scrape errors notify. `CHANGE_EPSILON = 0.005` in `database.py` means
anything under half a cent counts as unchanged. **A quiet day is the normal
case** - do not "fix" silence by re-enabling increases without weighing the
six-alerts-a-day cost.

## 7. Commands that actually work

Local Python lives in `.venv` (3.12). Two variables matter on Windows: without
them a Greek code page (cp1253) raises `UnicodeEncodeError` on the euro sign.

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING='utf-8'

.venv\Scripts\python.exe src\tracker.py --init-db    # create DB / print row count, no scraping
.venv\Scripts\python.exe src\tracker.py --dry-run    # scrape + store, log alerts instead of sending
.venv\Scripts\python.exe src\tracker.py --send-now   # scrape + store + REALLY send to Discord
run_tracker.cmd        # appends to logs\tracker.log; exit 0 ok / 1 target failed / 2 config error
"Send Prices Now.cmd"  # one-click --send-now with the output left visible
```

`--init-db` and `--dry-run` still touch `tracker.db` - see section 2.
`--send-now` sends real messages: run it only when the user asked for that.

## 8. Secrets

`.env` is gitignored and is **not** read by CI; the workflow uses repository
secrets (`DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Never
print, echo, log or commit a token or webhook URL, and never paste `.env`
contents into chat.

## 9. How to work on this repo

- Plan in plain English first, wait for an explicit "Go", and list every file
  you will touch. Three files is the ceiling for one step.
- No new libraries. The stack is the standard library plus `requests`,
  `beautifulsoup4` and `playwright` (see `requirements.txt`).
- Prefer small surgical edits over rewriting whole files.
- End every change with how to verify it. For anything that runs in CI the
  proof is an artifact - a bot commit, a row count, a log line - not a click.
- Update `README.md` after a feature, written for someone reading it in three
  months.

## 10. Environment traps already seen here

- Shell commands in this workspace report exit code 1 even when they succeed.
  Read the output; do not trust the exit code.
- PowerShell has no bash heredocs. Write the script to `%TEMP%` and run it.
- Delete scratch files from `%TEMP%` when done. No `_x.py` litter in the repo.
- A long command can leave the terminal busy; re-run a short read command
  instead of assuming failure.
- `git status` may report `tracker.db` as modified after some tools touch it.
  Check `git status --porcelain` before concluding a run wrote to it.
