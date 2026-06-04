import os
import traceback
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("SOURCE_GUILD_ID")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def load_cogs():
    # await bot.load_extension("src.cogs.room_commands")
    # await bot.load_extension("src.cogs.voice_listener")
    # await bot.load_extension("src.cogs.chat_listener")
    # await bot.load_extension("src.cogs.recruit_listener")
    await bot.load_extension("src.cogs.logging_cog")

@bot.event
async def on_ready():
    print(f"✅ 봇 준비 완료: {bot.user} (ID: {bot.user.id})")


async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
