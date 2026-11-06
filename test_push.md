# Push test

Pushing a change to **this file** runs the *Daily Price Check* workflow
immediately with `--send-now`, so the current price of every product in
`targets.json` is posted to Discord / Telegram straight away instead of
waiting for the 08:00 UTC schedule. That flag matters: a normal run stays
silent when no price moved, so without it a push would deliver nothing.

How to use it:

1. Change the date at the bottom (one character is enough).
2. Commit and push.
3. Open **Actions → Daily Price Check**, watch the run, then check Discord.

If the run fails with "no channel configured", the `DISCORD_WEBHOOK_URL`
repository secret is still missing — see section 7 of `README.md`.

Last tested: 2026-11-06 18:15 - heartbeat run (SEND_NOW=true, no skip-ci marker)
