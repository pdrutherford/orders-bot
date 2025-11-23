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
WINDOW_HOURS        = int(os.environ.get("WINDOW_HOURS", "24") or "24")   # scan window
CONCURRENCY         = int(os.environ.get("CONCURRENCY", "10") or "10")    # parallel channel scans

# Optional allowlists (comma-separated channel/category IDs)
ALLOW_CHANNEL_IDS_RAW   = os.environ.get("ALLOW_CHANNEL_IDS", "")
ALLOW_CATEGORY_IDS_RAW  = os.environ.get("ALLOW_CATEGORY_IDS", "")

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

ACK_USER_IDS: List[int] = _parse_id_list(ACK_USER_IDS_RAW, required=True, name="DISCORD_ACK_USER_IDS")
ALLOW_CHANNEL_IDS: List[int] = _parse_id_list(ALLOW_CHANNEL_IDS_RAW, required=False, name="ALLOW_CHANNEL_IDS")
ALLOW_CATEGORY_IDS: List[int] = _parse_id_list(ALLOW_CATEGORY_IDS_RAW, required=False, name="ALLOW_CATEGORY_IDS")

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
        # If allowlists are empty, allow everything
        if not ALLOW_CHANNEL_IDS and not ALLOW_CATEGORY_IDS:
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
