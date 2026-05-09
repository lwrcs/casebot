import asyncio
import logging

import discord

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.members = True  # required for on_member_join
intents.reactions = True

client = discord.Client(intents=intents)

CHUNK_SIZE = 1900


async def send_dm(content: str, discord_user_id: str | int,
                  channel: discord.abc.Messageable | None = None) -> bool:
    """Send a message to a user. Uses an existing channel if provided (faster inside on_message),
    otherwise fetches user by ID. Chunks long content. Returns True on success."""
    if not content:
        return True

    target = channel
    if target is None:
        try:
            target = await client.fetch_user(int(discord_user_id))
        except Exception:
            logger.exception(f"Failed to fetch Discord user {discord_user_id}")
            return False

    chunks = [content[i:i + CHUNK_SIZE] for i in range(0, len(content), CHUNK_SIZE)]

    for chunk in chunks:
        for attempt in range(2):
            try:
                await target.send(chunk)
                break
            except discord.errors.HTTPException as e:
                if attempt == 0:
                    logger.warning(f"Discord send failed ({e}); retrying in 2s")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"Discord send failed permanently for {discord_user_id}: {e}")
                    return False
            except Exception:
                logger.exception(f"Unexpected Discord send error for {discord_user_id}")
                return False
    return True
