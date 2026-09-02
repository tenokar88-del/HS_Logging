"""
익명채널 cog.

지정된 채널(익명채널)에 [채팅 입력] / [닉네임 변경] 버튼이 달린 '버튼 메시지'를
항상 채널 맨 아래에 유지한다.

- [채팅 입력]: 모달로 내용을 입력받아, 등록된 닉네임과 함께
  "## {닉네임}\n```{내용}```" 형식으로 채널에 게시한다.
- [닉네임 변경]: 모달로 새 닉네임을 입력받아 IDNICKMAP 테이블에 반영하고,
  기존에 등록된 유저였다면 변경 내역을 채팅 형식으로 채널에 남긴다.

실제 게시는 "버튼 메시지 바로 위의 봇이 쓴 마지막 메시지(타겟 메시지)"에
이어붙이는 방식이며, 타겟 메시지가 없거나(=바로 위가 사람이 쓴 메시지이거나
채널 맨 위) 길이 제한(1900자)을 넘으면 버튼 메시지 자체를 채팅으로 바꾸고
새 버튼 메시지를 다시 생성한다.

또한 이 채널에 (관리자 등) 사람이 직접 메시지를 올리면, 기존 버튼 메시지를
지우고 새 버튼 메시지를 맨 아래에 다시 생성해 항상 버튼이 맨 아래 있도록 한다.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands

from src import db

log = logging.getLogger(__name__)

# 환경변수로 덮어쓸 수 있도록 하되, 기본값은 요청하신 채널 ID로 둡니다.
ANONYMOUS_CHANNEL_ID = int(os.getenv("ANONYMOUS_CHANNEL_ID", "1544603117507846185"))

CHAT_INPUT_CUSTOM_ID = "anon_channel:chat_input"
NICKNAME_CHANGE_CUSTOM_ID = "anon_channel:nickname_change"

MAX_TARGET_LENGTH = 1900  # 이 길이를 넘으면 타겟 메시지에 이어붙이지 않고 새로 분리
MARKDOWN_STRIP_CHARS = "-`*_~|><@#"

# 버튼 메시지 편집/삭제·생성이 겹치지 않도록 하는 락 (동시 클릭 등 레이스 컨디션 방지)
_anon_lock = asyncio.Lock()


def strip_markdown(text: str) -> str:
    """닉네임 문자열에서 마크다운 특수문자(- ` * _ ~ | > < @ #)를 모두 제거합니다."""
    return text.translate(str.maketrans("", "", MARKDOWN_STRIP_CHARS)).strip()


def is_button_message(message: discord.Message, bot_user_id: int) -> bool:
    """이 메시지가 [채팅 입력]/[닉네임 변경] 버튼 2개가 달린 '버튼 메시지'인지 확인합니다."""
    if message.author.id != bot_user_id:
        return False
    if message.content:
        return False
    custom_ids = {
        child.custom_id
        for row in message.components
        for child in row.children
        if getattr(child, "custom_id", None)
    }
    return {CHAT_INPUT_CUSTOM_ID, NICKNAME_CHANGE_CUSTOM_ID}.issubset(custom_ids)


class ButtonMessageView(discord.ui.View):
    """항상 채널 맨 아래에 유지되는, 버튼 2개짜리 persistent view."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="채팅 입력",
        style=discord.ButtonStyle.primary,
        custom_id=CHAT_INPUT_CUSTOM_ID,
    )
    async def chat_input(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChatInputModal(button_message=interaction.message))

    @discord.ui.button(
        label="닉네임 변경",
        style=discord.ButtonStyle.secondary,
        custom_id=NICKNAME_CHANGE_CUSTOM_ID,
    )
    async def nickname_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NicknameChangeModal(button_message=interaction.message))


class ChatInputModal(discord.ui.Modal, title="채팅 입력"):
    content = discord.ui.TextInput(
        label="채팅 내용을 입력하세요",
        style=discord.TextStyle.paragraph,
        max_length=1800,
        required=True,
    )

    def __init__(self, button_message: discord.Message):
        super().__init__()
        self.button_message = button_message

    async def on_submit(self, interaction: discord.Interaction):
        cleaned_content = self.content.value.replace("`", "")
        nickname = await get_nickname(interaction.user.id)
        formatted = f"## {nickname}\n```{cleaned_content}```"

        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            await post_or_append(interaction.client, self.button_message, formatted)
        except discord.HTTPException:
            log.exception("익명채팅 메시지 처리 중 오류")
            await interaction.followup.send("메시지 전송 중 오류가 발생했습니다.", ephemeral=True)
            return
        await interaction.followup.send("전송되었습니다.", ephemeral=True)


class NicknameChangeModal(discord.ui.Modal, title="닉네임 변경"):
    nickname = discord.ui.TextInput(
        label="변경할 닉네임을 입력하세요.",
        style=discord.TextStyle.short,
        max_length=32,
        required=True,
    )

    def __init__(self, button_message: discord.Message):
        super().__init__()
        self.button_message = button_message

    async def on_submit(self, interaction: discord.Interaction):
        new_nickname = strip_markdown(self.nickname.value) or "ㅇㅇ"
        user_id = interaction.user.id

        row = await db.fetchone(
            "SELECT Nickname FROM IDNICKMAP WHERE UserID = %s", (user_id,)
        )

        if row is None:
            # 처음 등록하는 경우: 조용히 INSERT만 하고 채팅에는 아무것도 남기지 않는다.
            await db.execute(
                "INSERT INTO IDNICKMAP (UserID, Nickname) VALUES (%s, %s)",
                (user_id, new_nickname),
            )
            await interaction.response.send_message("닉네임이 등록되었습니다.", ephemeral=True)
            return

        # 이미 등록된 유저: UPDATE 하고 채팅에 변경 내역을 남긴다.
        old_nickname = row["Nickname"] or "ㅇㅇ"
        await db.execute(
            "UPDATE IDNICKMAP SET Nickname = %s WHERE UserID = %s",
            (new_nickname, user_id),
        )
        formatted = f"**닉네임 변경** : `{old_nickname}` --> `{new_nickname}`"

        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            await post_or_append(interaction.client, self.button_message, formatted)
        except discord.HTTPException:
            log.exception("닉네임 변경 메시지 처리 중 오류")
            await interaction.followup.send(
                "닉네임은 변경되었지만 채널 표시 중 오류가 발생했습니다.", ephemeral=True
            )
            return
        await interaction.followup.send("닉네임이 변경되었습니다.", ephemeral=True)


async def get_nickname(user_id: int) -> str:
    """IDNICKMAP에서 닉네임을 조회. 없거나 NULL이면 'ㅇㅇ'."""
    row = await db.fetchone(
        "SELECT Nickname FROM IDNICKMAP WHERE UserID = %s", (user_id,)
    )
    if row is None or row["Nickname"] is None:
        return "ㅇㅇ"
    return row["Nickname"]


async def post_or_append(
    bot: commands.Bot, button_message: discord.Message, formatted_text: str
) -> None:
    """
    '버튼 메시지' 바로 위의 '타겟 메시지'(봇이 쓴 마지막 채팅 취합 메시지)에
    새 내용을 이어붙이거나, 조건에 맞지 않으면 버튼 메시지를 채팅으로 바꾸고
    새 버튼 메시지를 다시 생성하는 공용 루틴.
    """
    async with _anon_lock:
        channel = button_message.channel

        target = None
        async for msg in channel.history(limit=1, before=button_message):
            target = msg

        can_append = (
            target is not None
            and target.author.id == bot.user.id
            and bool(target.content)
            and (len(target.content) + 1 + len(formatted_text)) <= MAX_TARGET_LENGTH
        )

        if can_append:
            await target.edit(content=f"{target.content}\n{formatted_text}")
            return

        # 타겟 메시지가 없거나(버튼 메시지 바로 위가 사람이 쓴 메시지 / 채널 맨 위)
        # 혹은 길이 초과 -> 버튼 메시지를 채팅 내용으로 전환하고 새 버튼 메시지 생성
        await button_message.edit(content=formatted_text, view=None)
        await channel.send(content=None, view=ButtonMessageView())


class AnonymousChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await db.init_pool()
        # persistent view는 custom_id 기준으로 동작하므로 메시지에 종속되지 않게 등록한다.
        self.bot.add_view(ButtonMessageView())

    @commands.Cog.listener()
    async def on_ready(self):
        channel = self.bot.get_channel(ANONYMOUS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ANONYMOUS_CHANNEL_ID)
            except discord.HTTPException:
                log.warning("익명채널(%s)을 찾을 수 없습니다.", ANONYMOUS_CHANNEL_ID)
                return

        async with _anon_lock:
            last_message = None
            async for msg in channel.history(limit=1):
                last_message = msg

            if last_message is None or not is_button_message(last_message, self.bot.user.id):
                await channel.send(content=None, view=ButtonMessageView())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id != ANONYMOUS_CHANNEL_ID:
            return
        if message.author.id == self.bot.user.id:
            return  # 봇 자신이 보낸 메시지(버튼/취합 메시지)는 무시

        # 버튼 메시지 이후에 사람(관리자 등)이 메시지를 올린 경우:
        # 기존 버튼 메시지를 지우고, 새 버튼 메시지를 맨 아래에 다시 생성한다.
        async with _anon_lock:
            prev = None
            async for msg in message.channel.history(limit=1, before=message):
                prev = msg

            if prev is not None and is_button_message(prev, self.bot.user.id):
                try:
                    await prev.delete()
                except discord.NotFound:
                    pass

            await message.channel.send(content=None, view=ButtonMessageView())


async def setup(bot: commands.Bot):
    await bot.add_cog(AnonymousChatCog(bot))
