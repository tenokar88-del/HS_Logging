"""
monthly_notice_cog.py
매달 1일, 관리자 채널에 "경고 차감 대상 유저 확인" 알림을 전송
"""

from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta, time as dtime

import discord
from discord.ext import commands, tasks

# 한국 시간 기준
KST = timezone(timedelta(hours=9))

# 매일 이 시각(KST)에 "오늘이 1일인지" 확인하고, 맞으면 알림을 보냄.
# 필요하면 환경변수로 시각을 바꿀 수 있음 (기본값: 매일 09:00 KST에 체크).
NOTICE_HOUR = int(os.getenv("MONTHLY_NOTICE_HOUR", 9))
NOTICE_MINUTE = int(os.getenv("MONTHLY_NOTICE_MINUTE", 0))
NOTICE_TIME = dtime(hour=NOTICE_HOUR, minute=NOTICE_MINUTE, tzinfo=KST)

NOTICE_MESSAGE = "새 달의 1일이 되었습니다. 경고 차감 대상 유저를 확인해 주세요."


def _load_admin_channel_id() -> int | None:
    val = os.getenv("ADMINISTRATORS_ROOM_ID")
    return int(val) if val else None


class MonthlyNoticeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.admin_channel_id: int | None = _load_admin_channel_id()
        self.check_monthly_notice.start()

    def cog_unload(self):
        self.check_monthly_notice.cancel()

    # tasks.loop(time=...)는 discord.py가 매일 지정된 시각(타임존 포함)에
    # 자동으로 실행해 줌. 여기서는 그 시각에 "오늘이 1일인지"만 확인.
    @tasks.loop(time=NOTICE_TIME)
    async def check_monthly_notice(self):
        now_kst = datetime.now(KST)
        if now_kst.day != 1:
            return  # 매달 1일이 아니면 아무 것도 하지 않음

        if not self.admin_channel_id:
            print("[MonthlyNotice] ADMINISTRATORS_ROOM_ID가 설정되지 않아 알림을 보낼 수 없습니다.")
            return

        channel = self.bot.get_channel(self.admin_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.admin_channel_id)
            except Exception as e:
                print(f"[MonthlyNotice] 채널을 찾을 수 없습니다 (id={self.admin_channel_id}): {e}")
                return

        try:
            await channel.send(NOTICE_MESSAGE)
        except Exception as e:
            print(f"[MonthlyNotice] 메시지 전송 실패: {e}")

    @check_monthly_notice.before_loop
    async def before_check_monthly_notice(self):
        # 봇이 완전히 준비될 때까지 대기 (get_channel/fetch_channel이 정상 동작하도록)
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MonthlyNoticeCog(bot))
