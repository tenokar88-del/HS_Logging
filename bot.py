import os
import traceback
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def load_cogs():
    await bot.load_extension("src.cogs.room_commands")
    await bot.load_extension("src.cogs.voice_listener")
    await bot.load_extension("src.cogs.chat_listener")
    await bot.load_extension("src.cogs.recruit_listener")
    await bot.load_extension("src.cogs.logging_cog")

@bot.event
async def on_ready():
    print(f"✅ 봇 준비 완료: {bot.user} (ID: {bot.user.id})")

    # 슬래시 커맨드 동기화
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"📌 서버({GUILD_ID})에 {len(synced)}개 커맨드 동기화 완료")
    else:
        synced = await bot.tree.sync()
        print(f"🌐 글로벌 {len(synced)}개 커맨드 동기화 완료 (최대 1시간 소요)")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    """슬래시 커맨드 오류를 터미널에 출력."""
    print("=" * 60)
    print(f"❌ 슬래시 커맨드 오류: /{interaction.command.name if interaction.command else '?'}")
    print(f"   유저: {interaction.user} (ID: {interaction.user.id})")
    traceback.print_exception(type(error), error, error.__traceback__)
    print("=" * 60)

    msg = f"❌ 오류 발생: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
