import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import discord

# ---------------------------
# Configuration from env
# ---------------------------
TOKEN               = os.environ["DISCORD_TOKEN"]
GUILD_ID            = int(os.environ["DISCORD_GUILD_ID"])
REPORT_CHANNEL_ID   = int(os.environ["REPORT_CHANNEL_ID"])        # where to post the report
ACK_USER_IDS_RAW    = os.environ.get("DISCORD_ACK_USER_IDS", "")  # comma-separated user IDs who can ✅

# Simplified knobs
WINDOW_HOURS        = int(os.environ.get("WINDOW_HOURS", "168") or "168")   # scan window
CONCURRENCY         = int(os.environ.get("CONCURRENCY", "10") or "10")    # parallel channel scans

# Optional allowlists (comma-separated channel/category IDs)
ALLOW_CHANNEL_IDS_RAW   = os.environ.get("ALLOW_CHANNEL_IDS", "")
ALLOW_CATEGORY_IDS_RAW  = os.environ.get("ALLOW_CATEGORY_IDS", "")

# Optional channel name filters (comma-separated channel names)
ALLOW_CHANNEL_NAMES_RAW = os.environ.get("ALLOW_CHANNEL_NAMES", "")
EXCLUDE_CHANNEL_NAMES_RAW = os.environ.get("EXCLUDE_CHANNEL_NAMES", "")

# Optional delivery phrase filtering (for scheduled runs)
REQUIRE_DELIVERY_PHRASE = os.environ.get("REQUIRE_DELIVERY_PHRASE", "").lower() in ("true", "1", "yes")

# Emoji matching: only Unicode scroll and check
SCROLL_UNICODE      = "📜"
CHECK_UNICODE       = "✅"

# Parse list of acknowledger IDs from env
def _parse_id_list(raw: str, required: bool = True, name: str = "ID list") -> List[int]:
    parts = [p.strip() for p in (raw or "").split(",")]
    ids: List[int] = []
    for p in parts:
        if not p:
            continue
        try:
            ids.append(int(p))
        except ValueError:
            raise ValueError(f"{name} contains a non-integer entry: '{p}'")
    if required and not ids:
        raise ValueError(f"{name} is empty; provide at least one ID")
    # de-dup preserve order
    seen = set()
    uniq: List[int] = []
    for i in ids:
        if i not in seen:
            uniq.append(i)
            seen.add(i)
    return uniq

def _parse_string_list(raw: str, name: str = "string list") -> List[str]:
    """Parse comma-separated list of strings (e.g., channel names)."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    # de-dup preserve order
    seen = set()
    uniq: List[str] = []
    for s in parts:
        if s not in seen:
            uniq.append(s)
            seen.add(s)
    return uniq

ACK_USER_IDS: List[int] = _parse_id_list(ACK_USER_IDS_RAW, required=True, name="DISCORD_ACK_USER_IDS")
ALLOW_CHANNEL_IDS: List[int] = _parse_id_list(ALLOW_CHANNEL_IDS_RAW, required=False, name="ALLOW_CHANNEL_IDS")
ALLOW_CATEGORY_IDS: List[int] = _parse_id_list(ALLOW_CATEGORY_IDS_RAW, required=False, name="ALLOW_CATEGORY_IDS")
ALLOW_CHANNEL_NAMES: List[str] = _parse_string_list(ALLOW_CHANNEL_NAMES_RAW, name="ALLOW_CHANNEL_NAMES")
EXCLUDE_CHANNEL_NAMES: List[str] = _parse_string_list(EXCLUDE_CHANNEL_NAMES_RAW, name="EXCLUDE_CHANNEL_NAMES")

# Safety: cap total buttons/messages to avoid spam on large servers
MAX_RESULTS         = int(os.environ.get("MAX_RESULTS", "500") or "500")   # overall cap
MAX_BUTTONS_PER_MSG = 25  # Discord: 5 rows * 5 buttons

# ---------------------------
# Discord client
# ---------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True  # required to read message content for 📜
client = discord.Client(intents=intents)

# ---------------------------
# Helpers
# ---------------------------

def _match_scroll_in_text(text: str) -> bool:
    """Return True if the Unicode scroll appears in the given text."""
    return bool(text) and (SCROLL_UNICODE in text)


def _matches_delivery_phrase(text: str) -> bool:
    """Return True if text starts with scroll emoji followed by 'delivery <month> <day> <time>' pattern matching today's date and current time slot.
    
    Pattern: '📜 delivery <month> <day> <time>' or ':scroll: delivery <month> <day> <time>'
    - month: 3-letter shorthand (jan, feb, etc.) or full name (january, february, etc.)
    - day: 1-2 digit number (1-31)
    - time: 'morning' or 'evening'
    
    When REQUIRE_DELIVERY_PHRASE is enabled (scheduled runs):
    - Matches only messages for today's date (in PST/PDT timezone)
    - 5am PST run: matches 'morning' deliveries
    - 5pm PST run: matches 'evening' deliveries
    """
    import re
    from zoneinfo import ZoneInfo
    
    if not text:
        return False
    
    # Parse the delivery phrase - must start with scroll emoji, then delivery phrase
    # Accept both 3-letter and full month names
    pattern = r'^📜\s*delivery\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})\s+(morning|evening)'
    match = re.match(pattern, text.strip().lower())
    if not match:
        return False
    
    month_str, day_str, time_slot = match.groups()
    
    # Get current time in PST/PDT (America/Los_Angeles)
    pst_tz = ZoneInfo("America/Los_Angeles")
    now_pst = datetime.now(pst_tz)
    
    # Map month names (both short and full) to numbers
    month_map = {
        'jan': 1, 'january': 1,
        'feb': 2, 'february': 2,
        'mar': 3, 'march': 3,
        'apr': 4, 'april': 4,
        'may': 5,
        'jun': 6, 'june': 6,
        'jul': 7, 'july': 7,
        'aug': 8, 'august': 8,
        'sep': 9, 'september': 9,
        'oct': 10, 'october': 10,
        'nov': 11, 'november': 11,
        'dec': 12, 'december': 12
    }
    
    message_month = month_map[month_str]
    message_day = int(day_str)
    
    # Check if message is for today
    if message_month != now_pst.month or message_day != now_pst.day:
        return False
    
    # Determine expected time slot based on current hour in PST
    # 5am PST run (hour 5): look for 'morning' deliveries
    # 5pm PST run (hour 17): look for 'evening' deliveries
    current_hour = now_pst.hour
    
    if 4 <= current_hour < 9:  # Morning run window (5am ±few hours)
        expected_slot = 'morning'
    elif 16 <= current_hour < 22:  # Evening run window (5pm ±few hours)
        expected_slot = 'evening'
    else:
        # Outside expected run windows - accept both to be safe
        return True
    
    return time_slot == expected_slot


async def _contains_scroll(msg: discord.Message) -> bool:
    # Check message content first (fast path)
    if _match_scroll_in_text(getattr(msg, "content", "") or ""):
        return True
    # Also check embed title/description text if present
    for e in msg.embeds:
        if _match_scroll_in_text((getattr(e, "title", "") or "")):
            return True
        if _match_scroll_in_text((getattr(e, "description", "") or "")):
            return True
    return False


async def _user_has_checkmark(msg: discord.Message) -> bool:
    """Return True if any configured acknowledger has reacted with the ✅ emoji on this message."""
    # Fast path: if no ✅ reaction on the message at all, user can't be among them.
    check_reaction = next((r for r in msg.reactions if str(r.emoji) == CHECK_UNICODE), None)
    if not check_reaction:
        return False
    # Otherwise, enumerate ✅ users and stop early if any configured ID is present
    ack_set = set(ACK_USER_IDS)
    async for u in check_reaction.users(limit=None):
        if u.id in ack_set:
            return True
    return False


def _list_messageables(guild: discord.Guild):
    """Return channels/threads to scan (no archived thread fetching)."""
    items: List[discord.abc.Messageable] = []
    
    # Helper to check if a channel passes allowlist filters
    def _passes_filters(ch) -> bool:
        channel_name = getattr(ch, "name", None) or ""
        
        # Apply exclude list first (takes priority)
        if EXCLUDE_CHANNEL_NAMES:
            if channel_name in EXCLUDE_CHANNEL_NAMES:
                return False
        
        # If no filters at all, allow everything
        has_any_filter = (ALLOW_CHANNEL_IDS or ALLOW_CATEGORY_IDS or ALLOW_CHANNEL_NAMES)
        if not has_any_filter:
            return True
        
        # Check channel name allowlist
        if ALLOW_CHANNEL_NAMES and channel_name in ALLOW_CHANNEL_NAMES:
            return True
        
        # Check channel ID allowlist
        if ALLOW_CHANNEL_IDS and ch.id in ALLOW_CHANNEL_IDS:
            return True
        
        # Check category ID allowlist (if channel has a category)
        if ALLOW_CATEGORY_IDS:
            category_id = getattr(ch, "category_id", None)
            if category_id and category_id in ALLOW_CATEGORY_IDS:
                return True
        
        # If allowlists are set but channel doesn't match, reject
        return False
    
    # Text channels
    for ch in guild.text_channels:
        if _passes_filters(ch):
            items.append(ch)
            # Active threads in allowed text channels (includes private threads)
            items.extend(ch.threads)
    
    # Active forum threads (if forums are present)
    for forum in (getattr(guild, "forums", None) or getattr(guild, "forum_channels", [])):
        if _passes_filters(forum):
            items.extend(forum.threads)
    
    return items


async def _scan(guild: discord.Guild) -> List[Dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    messageables = _list_messageables(guild)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def scan_one(ch: discord.abc.Messageable) -> List[Dict]:
        rows: List[Dict] = []
        try:
            async with sem:
                async for msg in ch.history(limit=None, after=since, oldest_first=False):
                    # Look for 📜
                    if not await _contains_scroll(msg):
                        continue

                    # If any acknowledger already reacted with ✅, skip
                    if await _user_has_checkmark(msg):
                        continue

                    # If delivery phrase filtering is enabled, check the pattern
                    if REQUIRE_DELIVERY_PHRASE:
                        if not _matches_delivery_phrase(msg.content or ""):
                            continue

                    # Otherwise record the message
                    rows.append({
                        "channel": f"#{getattr(msg.channel, 'name', msg.channel.id)}",
                        "created_at_utc": msg.created_at.replace(tzinfo=timezone.utc).isoformat(timespec="minutes"),
                        "jump_url": msg.jump_url,
                        "preview": ((msg.content or "").replace("\n", " ").strip())[:140] or "(no text)",
                    })

                    # Keep per-channel work bounded too
                    if len(rows) >= MAX_RESULTS:
                        break
        except discord.Forbidden:
            # Skip channels we can't read (shouldn't happen for admins)
            pass
        except discord.HTTPException:
            # Skip on other API errors
            pass
        return rows

    # Run scans concurrently with a cap
    results_lists = await asyncio.gather(*(scan_one(ch) for ch in messageables))

    # Flatten and enforce global cap
    results = [r for sub in results_lists for r in sub]
    if len(results) > MAX_RESULTS:
        results = results[:MAX_RESULTS]

    return results


def _chunk_buttons(rows: List[Dict], chunk_size: int = MAX_BUTTONS_PER_MSG):
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i+chunk_size]
        view = discord.ui.View()
        for r in chunk:
            # Button label: "#channel · HH:MM • preview" (<= 80 chars)
            hhmm = r["created_at_utc"][11:16]  # naive slice from ISO
            label = f'{r["channel"]} · {hhmm} • {r["preview"]}'
            if len(label) > 80:
                label = label[:77] + "…"
            view.add_item(discord.ui.Button(
                label=label,
                url=r["jump_url"],
                style=discord.ButtonStyle.link
            ))
        yield view


async def _post_results(channel: discord.abc.Messageable, results: List[Dict]) -> None:
    total = len(results)
    if total == 0:
        embed = discord.Embed(
            title="Unacknowledged scrolls",
            description=f"No matching messages in the last {WINDOW_HOURS} hours. 🎉",
        )
        await channel.send(embed=embed)
        return

    # Intro embed
    embed = discord.Embed(
        title="Unacknowledged scrolls",
        description=f"Tap a button to jump to a message. Window: last {WINDOW_HOURS} hours.",
    )
    embed.set_footer(text=f"Total: {total}")
    await channel.send(embed=embed)

    # Then the buttons (25 per message)
    for view in _chunk_buttons(results):
        await channel.send(view=view)

# ---------------------------
# Entrypoint
# ---------------------------
async def _main():
    await client.wait_until_ready()
    guild = client.get_guild(GUILD_ID)
    if not guild:
        print("Guild not found or bot not in guild.")
        await client.close()
        return

    results = await _scan(guild)

    ch = client.get_channel(REPORT_CHANNEL_ID)
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        print("Report channel not found or wrong type.")
    else:
        await _post_results(ch, results)

    await client.close()


@client.event
async def on_ready():
    # One-shot run
    asyncio.create_task(_main())


if __name__ == "__main__":
    client.run(TOKEN)
