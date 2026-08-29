# Telegram Facebook UID Status Monitor

## Main behavior
- Recovery and Deletion monitors are separate.
- Playwright checks the rendered Facebook profile page every 5 minutes.
- The scheduled checker is SILENT when nothing changed.
- A Telegram notification is sent only when the detected status changes, e.g. UNKNOWN -> LIVE or LIVE -> DEAD.
- `/scan` is also silent when there are no status changes.
- `/addrecovery` and `/adddeletion` perform an immediate first check.

## UID information
Each monitored UID can store:
- Issue / case name
- Amount
- Owner
- Details / notes
- Last detected status
- Last checked time
- Facebook rendered-text marker
- HTTP status

Use:
`/setinfo recovery UID | Issue | Amount | Owner | Details`

Example:
`/setinfo recovery 100070780590181 | Codilla Suspended Acc | 2500 | Karl | Recovery case; check appeal status`

## Commands
- `/addrecovery 123456789`
- `/adddeletion 123456789`
- `/setinfo recovery UID | Issue | Amount | Owner | Details`
- `/setinfo deletion UID | Issue | Amount | Owner | Details`
- `/scan`
- `/list`
- `/remove recovery UID`
- `/remove deletion UID`
- `/help`

Each UID result includes inline buttons for Update Info, List of UIDs, Delete UID, and Open Profile.

## Detection note
The detector uses the rendered Facebook page text and common unavailable-page markers. LIVE/DEAD is a signal and is not a guaranteed determination of whether Facebook has disabled, suspended, locked, restricted, or restored an account.

## Security
BOT_TOKEN is a credential. If the token has been shared publicly, regenerate it with BotFather and replace it in config.py.
