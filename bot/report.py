import os
import io
import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Dict, List, Iterable, Union

import discord

# ---------------------------
# Configuration from env
# ---------------------------
TOKEN               = os.environ["DISCORD_TOKEN"]
GUILD_ID            = int(os.environ["DISCORD_GUILD_ID"])
TARGET_USER_ID      = int(os.environ["DISCORD_TARGET_USER_ID"])   # "you" (the human who must add ✅)
REPORT_CHANNEL_ID   = int(os.environ["REPORT_CHANNEL_ID"])        # where to post the report

# Optional knobs (have sensibly safe defaults)
WINDOW_HOURS        = int(os.environ.get("WINDOW_HOURS", "24") or "24")   # scan window
INCLUDE_BOTS        = os.environ.get("INCLUDE_BOTS", "false").lower() == "true"
DRY_RUN             = os.environ.get("DRY_RUN", "false").lower() == "true"
# Debug logging for channels/threads being scanned
DEBUG_LOG_CHANNELS  = os.environ.get("DEBUG_LOG_CHANNELS", "false").lower() == "true"

# Emoji matching:
# If CUSTOM_SCROLL_ID is set (e.g. 123456789012345678), we'll also match "<:scroll:ID>"
CUSTOM_SCROLL_ID    = os.environ.get("CUSTOM_SCROLL_ID")          # None or numeric str
SCROLL_UNICODE      = os.environ.get("SCROLL_UNICODE", "📜")      # default to the Unicode scroll
CHECK_UNICODE       = os.environ.get("CHECK_UNICODE", "✅")

# Channel/category allowlists (comma-separated numeric IDs). If empty, scan all.
ALLOW_CHANNEL_IDS   = [int(x) for x in os.environ.get("ALLOW_CHANNEL_IDS", "").split(",") if x.strip().isdigit()]
ALLOW_CATEGORY_IDS  = [int(x) for x in os.environ.get("ALLOW_CATEGORY_IDS", "").split(",") if x.strip().isdigit()]

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
    if not text:
        return False
    if SCROLL_UNICODE and SCROLL_UNICODE in text:
        return True
    if CUSTOM_SCROLL_ID:
        # Minimal match for custom <:scroll:ID>
        needle = f"<:scroll:{CUSTOM_SCROLL_ID}>"
        if needle in text:
            return True
        # Some clients omit the name or rename it; fallback to just ID form if present
        # (less strict, but helps if the emoji name changed)
        if f":{CUSTOM_SCROLL_ID}>" in text and "<:" in text:
            return True
    return False

async def _contains_scroll(msg: discord.Message) -> bool:
    if _match_scroll_in_text(getattr(msg, "content", None) or ""):
        return True
    # Also check embed title/description text if present
    for e in msg.embeds:
        if _match_scroll_in_text((getattr(e, "title", "") or "")):
            return True
        if _match_scroll_in_text((getattr(e, "description", "") or "")):
            return True
    return False

async def _user_has_checkmark(msg: discord.Message) -> bool:
    # Fast path: if no ✅ reaction on the message at all, user can't be among them.
    check_reaction = next((r for r in msg.reactions if str(r.emoji) == CHECK_UNICODE), None)
    if not check_reaction:
        return False
    # Otherwise, enumerate ✅ users and see if TARGET_USER_ID is present
    async for u in check_reaction.users(limit=None):
        if u.id == TARGET_USER_ID:
            return True
    return False


def _describe_messageable(ch: Union[discord.abc.GuildChannel, discord.Thread]) -> str:
    """Human-friendly description for debug logging."""
    try:
        if isinstance(ch, discord.Thread):
            parent = getattr(ch, "parent", None)
            parent_name = getattr(parent, "name", None)
            parent_disp = f"#{parent_name}" if parent_name else f"parent_id={getattr(parent, 'id', 'unknown')}"
            th_name = getattr(ch, "name", None) or str(getattr(ch, "id", "unknown"))
            return f"Thread '{th_name}' in {parent_disp} (id={getattr(ch, 'id', 'unknown')})"
        # TextChannel or other GuildChannel
        name = getattr(ch, "name", None)
        if isinstance(ch, discord.TextChannel):
            disp = f"#{name}" if name else str(getattr(ch, 'id', 'unknown'))
        else:
            disp = name or str(getattr(ch, 'id', 'unknown'))
        return f"Channel {disp} (id={getattr(ch, 'id', 'unknown')})"
    except Exception:
        return f"<messageable id={getattr(ch, 'id', 'unknown')}>"

async def _iter_textish(guild: discord.Guild) -> AsyncIterator[discord.abc.Messageable]:
    """Yield text channels + forum channels' threads + archived public threads."""
    # Standard text channels
    for ch in guild.text_channels:
        if _channel_allowed(ch):
            yield ch
            # Active threads in text channels
            for th in ch.threads:
                if _channel_allowed(th):
                    yield th
            # Archived public threads (private archived need perms & private=True fetch)
            try:
                async for th in ch.archived_threads(limit=None, private=False):
                    if _channel_allowed(th):
                        yield th
            except discord.Forbidden:
                # Skip archived threads if we don't have permission
                continue
            except discord.HTTPException:
                # Skip on other API errors
                continue
    # Forum channels: iterate their threads as well
    for forum in (getattr(guild, "forums", None) or getattr(guild, "forum_channels", [])):
        if not _channel_allowed(forum):
            continue
        # Active forum threads
        for th in forum.threads:
            if _channel_allowed(th):
                yield th
        # Archived public forum threads
        try:
            async for th in forum.archived_threads(limit=None, private=False):
                if _channel_allowed(th):
                    yield th
        except discord.Forbidden:
            # Skip archived threads if we don't have permission
            continue
        except discord.HTTPException:
            # Skip on other API errors
            continue

async def _scan(guild: discord.Guild) -> List[Dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    results: List[Dict] = []
    scanned = 0

    async for ch in _iter_textish(guild):
        if DEBUG_LOG_CHANNELS:
            try:
                print(f"Scanning { _describe_messageable(ch) }")
            except Exception:
                # Avoid debug causing failures
                pass
        try:
            async for msg in ch.history(limit=None, after=since, oldest_first=False):
                scanned += 1
                if not INCLUDE_BOTS and getattr(msg.author, "bot", False):
                    continue
                if await _contains_scroll(msg) and not await _user_has_checkmark(msg):
                    results.append({
                        "channel": f"#{getattr(msg.channel, 'name', msg.channel.id)}",
                        "created_at_utc": msg.created_at.replace(tzinfo=timezone.utc).isoformat(timespec="minutes"),
                        "jump_url": msg.jump_url,
                        "preview": ((msg.content or "").replace("\n", " ").strip())[:140] or "(no text)",
                    })
                    if len(results) >= MAX_RESULTS:
                        return results
        except discord.Forbidden:
            if DEBUG_LOG_CHANNELS:
                try:
                    print(f"Skipping (forbidden): { _describe_messageable(ch) }")
                except Exception:
                    pass
            continue
        except discord.HTTPException:
            if DEBUG_LOG_CHANNELS:
                try:
                    print(f"Skipping (HTTP error): { _describe_messageable(ch) }")
                except Exception:
                    pass
            continue

    return results

def _chunk_buttons(rows: List[Dict], chunk_size: int = MAX_BUTTONS_PER_MSG) -> Iterable[discord.ui.View]:
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
            title="📜 Unacknowledged scrolls",
            description=f"No matching messages in the last {WINDOW_HOURS} hours. 🎉",
        )
        await channel.send(embed=embed)
        return

    # Intro embed
    embed = discord.Embed(
        title="📜 Unacknowledged scrolls",
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

    # DRY RUN: print summary instead of posting
    if DRY_RUN:
        print(f"DRY RUN: {len(results)} unacknowledged scroll(s) in last {WINDOW_HOURS}h.")
        for r in results:
            print(f"- {r['created_at_utc']} {r['channel']}: {r['preview']} -> {r['jump_url']}")
        await client.close()
        return

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
