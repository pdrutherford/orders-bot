# Unacknowledged Scrolls Reporter (Discord)

Find all Discord messages in the last _N_ hours containing the 📜 emoji where no configured acknowledger has reacted with ✅, then post jump links (as buttons) into a channel.

## What it does

- Scans all text channels + threads (including archived public threads) in one guild.
- Matches Unicode 📜 by default; can also match a server custom emoji via `CUSTOM_SCROLL_ID`.
- For each match, verifies none of the configured acknowledgers have added a ✅ reaction.
- Posts an embed + link buttons directly in your chosen report channel.
- Runs in GitHub Actions on a schedule or manually.

## Setup

1. **Create a Discord bot**

   - In the Developer Portal, enable **MESSAGE CONTENT INTENT**.
   - Invite the bot to your server with at least:
     - _View Channels_, _Read Message History_ (server-wide for areas you want scanned)
     - _Send Messages_, _Embed Links_ (in the report channel)

2. **Repo secrets (Settings → Secrets and variables → Actions)**

   - `DISCORD_TOKEN` – Bot token
   - `DISCORD_GUILD_ID` – Guild (server) ID
   - `DISCORD_ACK_USER_IDS` – Comma-separated Discord user IDs allowed to acknowledge with ✅ (required)
   - `REPORT_CHANNEL_ID` – Channel (or thread) ID to receive reports
   - _(Optional)_ `CUSTOM_SCROLL_ID` – Numeric ID of a custom `:scroll:` emoji

3. **Repo variables (Settings → Variables)**

   - _(Optional)_ `SCROLL_UNICODE` – Defaults to 📜
   - _(Optional)_ `CHECK_UNICODE` – Defaults to ✅
   - _(Optional)_ `MAX_RESULTS` – Safety cap (default 500)
   - _(Optional)_ **Channel allowlists** – If empty, all channels are scanned. Define up to 3 sets:
     - `ALLOW_CHANNEL_IDS` / `ALLOW_CATEGORY_IDS` – Default set (used by scheduled runs)
     - `ALLOW_CHANNEL_IDS_SET1` / `ALLOW_CATEGORY_IDS_SET1` – Alternate set 1
     - `ALLOW_CHANNEL_IDS_SET2` / `ALLOW_CATEGORY_IDS_SET2` – Alternate set 2
     - Format: Comma-separated Discord IDs (e.g., `123456789012345678,987654321098765432`)
   - _(Optional)_ **Channel name filters** – Can be passed at runtime via manual workflow:
     - `ALLOW_CHANNEL_NAMES` – Comma-separated channel names to include (e.g., `general,announcements`)
     - `EXCLUDE_CHANNEL_NAMES` – Comma-separated channel names to exclude (e.g., `off-topic,bot-spam`)

4. **Schedule**
   - The workflow runs twice daily at **5:00 AM PST** and **5:00 PM PST**.
   - Scheduled runs target **only the #message-tracker channel**.
   - Messages must start with 📜 followed by the delivery phrase pattern for **today's date and time slot**:
     - **5am run**: Finds messages starting with `📜 delivery <month> <day> morning` (today's date)
     - **5pm run**: Finds messages starting with `📜 delivery <month> <day> evening` (today's date)
     - Month: 3-letter shorthand (jan, feb, mar, etc.) or full name (january, february, march, etc.)
     - Day: 1-2 digit number matching today's day of month
     - Example on Dec 12: `📜 delivery dec 12 morning`, `📜 Delivery December 12 evening`

## Manual run (override)

- In the Actions tab → **Unacknowledged Scrolls Report** → **Run workflow**
  - `window_hours` – How many hours back to scan (default: 168)
  - `allow_channel_names` – Specific channel names to include (comma-separated)
  - `exclude_channel_names` – Channel names to exclude (defaults to `message-tracker`; clear to include it)
  - Manual runs do **not** enforce the delivery phrase pattern or date/time matching
  - `window_hours` (e.g., `6`, `24`, `48`)
  - `allow_channel_names` – Comma-separated channel names to include (e.g., `general,announcements`)
  - `exclude_channel_names` – Comma-separated channel names to exclude (e.g., `off-topic,bot-spam`)

## Local testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r bot/requirements.txt
export DISCORD_TOKEN=... \
       DISCORD_GUILD_ID=... \
   DISCORD_ACK_USER_IDS=111111111111111111,222222222222222222 \
       REPORT_CHANNEL_ID=... \
       WINDOW_HOURS=24 \
       ALLOW_CHANNEL_IDS=123456789012345678,987654321098765432 \
       ALLOW_CHANNEL_NAMES=general,announcements \
       EXCLUDE_CHANNEL_NAMES=off-topic,bot-spam
python bot/report.py
```

## Notes

- Private archived threads require appropriate permissions (and code change to fetch `private=True`).
- If your server uses a custom `:scroll:` emoji, set `CUSTOM_SCROLL_ID` to its numeric ID.
- **Channel filtering behavior:**
  - Exclude lists take priority: if a channel name is in `EXCLUDE_CHANNEL_NAMES`, it's skipped regardless of other filters.
  - If any allow filters are set (`ALLOW_CHANNEL_IDS`, `ALLOW_CATEGORY_IDS`, or `ALLOW_CHANNEL_NAMES`), a channel must match at least one to be scanned.
  - Channel name filters work alongside ID-based filters: a channel matches if it passes any allow filter (name, channel ID, or category ID).
  - If no filters are configured, all channels in the guild are scanned.

---

## Acceptance criteria (so Copilot knows when it’s “done”)

- [ ] Workflow `unack_scrolls.yml` exists and runs on schedule + manual.
- [ ] Script logs in, scans last `WINDOW_HOURS`, and **posts link-button batches** to `REPORT_CHANNEL_ID`.
- [ ] Matches **Unicode 📜** and (if provided) **`CUSTOM_SCROLL_ID`** occurrences.
- [ ] Correctly **excludes** messages already reacted to with ✅ by any ID in **`DISCORD_ACK_USER_IDS`**.
- [ ] Respects allowlists (`ALLOW_CHANNEL_IDS`, `ALLOW_CATEGORY_IDS`) if set.
- [ ] No CSV files; output is **embed + buttons only**.
- [ ] Gracefully skips channels without permission and survives HTTP hiccups.
- [ ] Works from a cold start in GitHub Actions (no long-lived process).

If you want me to pre-fill your IDs and cron in these files before you paste them into the repo, just drop the values and I’ll slot them in.
