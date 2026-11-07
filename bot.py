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
    await bot.load_extension("src.cogs.logging_cog")
    await bot.load_extension("src.cogs.monthly_notice_cog")
    await bot.load_extension("src.cogs.anonymous_chat_cog")
    await bot.load_extension("src.cogs.countdown_cog")


async def setup_hook():
    """로그인 직후, 게이트웨이 연결 전에 실행됨. 슬래시 명령어를 디스코드에 등록(sync)한다.
    (main()에서 bot.start 전에 sync하면 로그인 전이라 application_id가 없어 오류가 남)"""
    if GUILD_ID:
        # SOURCE_GUILD_ID 서버에만 등록 — 서버에 바로 반영됨
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    else:
        # 서버 ID가 없으면 전체 등록 (반영까지 시간이 걸릴 수 있음)
        synced = await bot.tree.sync()
    print(f"✅ 슬래시 명령어 {len(synced)}개 동기화: {[c.name for c in synced]}")


bot.setup_hook = setup_hook


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