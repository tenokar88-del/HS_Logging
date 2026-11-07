"""
countdown_cog.py
/타이머 {초} — 채널에 이모지 카운트다운 메시지를 만들고 5초마다 갱신

- 채널에 이모지 시계 메시지 1개만 사용 (예: 2️⃣0️⃣<:hs_colon:...>0️⃣0️⃣). 이 메시지의 id 가 message_id.
- 카운트다운 정보는 DB(countdowntimer)와 봇 메모리(self.timers)에 같이 보관.
  갱신할 때는 DB를 조회하지 않고 메모리만 사용한다.
- 전역 변수 countdown_switch: 0이면 갱신 루프가 아무것도 하지 않고, 1이면 동작.

DB 접근은 db.py(psycopg3 풀 헬퍼)의 fetchall / execute 를 사용한다.
(db.py 에 fetchall 이 추가된 버전이 필요함)
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

# db.py 위치에 맞게 고칠 것: anonymous_chat_cog.py 맨 위의 db import 줄과 똑같이 맞추면 됨
from src import db

# ── 설정 ──────────────────────────────────────────────────────────
UPDATE_INTERVAL = 5                  # 한 채널의 기본(최소) 갱신 주기(초)
# 채널당 수정 한도를 "1초에 1건" 정도로 보수적으로 가정 (디스코드는 5초에 5건 안팎으로 알려져 있음).
# 타이머 1개 = 수정 1건이므로, 한 채널의 갱신 주기는
#   max(UPDATE_INTERVAL, 타이머 수 × EDIT_GAP) 초 → 타이머 5개까지는 5초, 8개면 8초.
EDIT_GAP = 1.0
SCHEDULER_TICK = 1                   # 어느 채널을 갱신할 차례인지 확인하는 주기(초). 메모리만 보므로 부담 없음.
MAX_SECONDS = 7 * 86400              # /타이머 에 넣을 수 있는 최대 초 (7일)
MAX_TIMERS_PER_CHANNEL = 3           # 한 채널에서 동시에 돌릴 수 있는 타이머 수
COLON = "<:hs_colon:1568412447646421042>"   # 서버 이모지 (콜론)
END_MESSAGE = "{mention} 타이머가 종료되었습니다! ⏰"   # 종료 시 보낼 메시지 ({mention} = 만든 사람 멘션)

# 테이블/컬럼 이름이 다르면 여기만 고치면 됨
SQL_SELECT_ALL = (
    "SELECT message_id, channel_id, creator_id, end_timestamp "
    "FROM countdowntimer"
)
SQL_INSERT = (
    "INSERT INTO countdowntimer "
    "(message_id, channel_id, creator_id, end_timestamp) "
    "VALUES (%s, %s, %s, %s)"
)
SQL_DELETE = "DELETE FROM countdowntimer WHERE message_id = %s"

# 카운트다운이 하나라도 있으면 1, 없으면 0
countdown_switch = 0


# ── 표시 형식 ─────────────────────────────────────────────────────

def _emoji_digits(n: int) -> str:
    """정수를 키캡 숫자 이모지로 변환. 12 → 1️⃣2️⃣
    (API로 보낸 ':two:' 텍스트는 디스코드가 이모지로 바꿔주지 않으므로 유니코드 키캡을 직접 사용)"""
    return "".join(f"{d}️⃣" for d in str(n))


def format_clock(remaining: int) -> str:
    """남은 초 → M:ss 이모지 문자열. 분은 앞에 0을 붙이지 않고, 60분 이상도 분으로만 표시."""
    remaining = max(0, remaining)
    minutes, secs = divmod(remaining, 60)
    return (
        _emoji_digits(minutes)
        + COLON
        + _emoji_digits(secs // 10)
        + _emoji_digits(secs % 10)
    )


@dataclass
class Timer:
    message_id: int
    channel_id: int
    creator_id: int
    end_timestamp: int   # 유닉스 초


class CountdownCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.timers: dict[int, Timer] = {}   # message_id → Timer
        # 채널별 갱신 상태
        self._last_run: dict[int, float] = {}    # channel_id → 마지막 갱신 시작 시각(monotonic)
        self._running: set[int] = set()          # 지금 갱신 작업이 돌고 있는 channel_id
        self._tasks: set[asyncio.Task] = set()   # 실행 중인 갱신 작업 (GC 방지용 참조)
        # 만드는 중인 타이머 수 (동시에 /타이머 를 여러 번 눌러도 채널당 한도를 넘지 않게 선점)
        self._creating: dict[int, int] = {}      # channel_id → 생성 진행 중인 개수

    # ── 시작/종료 ─────────────────────────────────────────────────

    async def cog_load(self):
        """봇 시작(재부팅) 시 DB를 한 번만 읽어서 메모리에 채운다."""
        global countdown_switch
        try:
            rows = await db.fetchall(SQL_SELECT_ALL)
        except Exception as e:
            # DB 연결에 문제가 있어도 cog 로드는 계속 진행 (다른 기능/슬래시 명령어 등록이 막히지 않게).
            # 이 경우 기존 타이머는 메모리에 없으므로 DB를 고친 뒤 봇을 재시작해야 이어진다.
            print(f"[Countdown] ⚠️ DB에서 타이머를 읽지 못했습니다. 빈 상태로 시작합니다: {e!r}")
            rows = []
        for r in rows:
            self.timers[r["message_id"]] = Timer(
                message_id=r["message_id"],
                channel_id=r["channel_id"],
                creator_id=r["creator_id"],
                end_timestamp=r["end_timestamp"],
            )
        countdown_switch = 1 if self.timers else 0
        print(f"[Countdown] DB에서 {len(self.timers)}개 로드, countdown_switch={countdown_switch}")
        self.update_loop.start()

    async def cog_unload(self):
        self.update_loop.cancel()
        for task in list(self._tasks):
            task.cancel()

    # ── 내부 도우미 ───────────────────────────────────────────────

    def _partial(self, channel_id: int, message_id: int) -> discord.PartialMessage:
        """캐시 없이 메시지를 다루기 위한 PartialMessage."""
        channel = self.bot.get_partial_messageable(channel_id)
        return channel.get_partial_message(message_id)

    async def _remove(self, timer: Timer):
        """메모리와 DB에서 카운트다운 정보를 삭제. 메모리가 비면 스위치 off."""
        global countdown_switch
        self.timers.pop(timer.message_id, None)
        if not self.timers:
            countdown_switch = 0
        try:
            await db.execute(SQL_DELETE, (timer.message_id,))
        except Exception as e:
            # DB 삭제에 실패해도 메모리에서는 이미 지워짐.
            # (재부팅하면 다시 로드되지만, 이미 만료 상태면 첫 갱신 때 바로 정리됨)
            print(f"[Countdown] DB 삭제 실패 (message_id={timer.message_id}): {e}")

    # ── 갱신 스케줄러 ─────────────────────────────────────────────
    # 1초마다 "어느 채널을 갱신할 차례인지"만 메모리에서 확인하고,
    # 차례가 된 채널은 별도 작업으로 띄운다. 채널끼리는 서로 기다리지 않는다.
    # (수정 한도가 채널 단위라서, 한 채널이 한도에 걸려 기다려도 다른 채널은 영향 없음)

    @tasks.loop(seconds=SCHEDULER_TICK)
    async def update_loop(self):
        if countdown_switch != 1:
            return

        by_channel: dict[int, list[Timer]] = {}
        for timer in self.timers.values():
            by_channel.setdefault(timer.channel_id, []).append(timer)

        # 타이머가 없어진 채널의 상태 정리
        for channel_id in list(self._last_run):
            if channel_id not in by_channel:
                del self._last_run[channel_id]

        mono = time.monotonic()
        wall = time.time()
        for channel_id, channel_timers in by_channel.items():
            if channel_id in self._running:      # 이전 갱신이 아직 진행 중
                continue

            interval = max(UPDATE_INTERVAL, len(channel_timers) * EDIT_GAP)
            if mono - self._last_run.get(channel_id, float("-inf")) >= interval:
                # 이 채널의 정규 갱신 차례: 채널의 모든 타이머 갱신
                self._last_run[channel_id] = mono
                self._spawn(channel_id, channel_timers)
            else:
                # 차례가 아니어도 이미 끝난 타이머는 바로 마무리 (종료가 주기만큼 늦어지지 않게)
                expired = [t for t in channel_timers if t.end_timestamp <= wall]
                if expired:
                    self._spawn(channel_id, expired)

    def _spawn(self, channel_id: int, channel_timers: list[Timer]):
        self._running.add(channel_id)
        task = asyncio.create_task(self._update_channel(channel_id, channel_timers))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _update_channel(self, channel_id: int, channel_timers: list[Timer]):
        """한 채널의 타이머들을 차례로 갱신. 오류가 나도 다른 채널/다음 주기에는 영향 없음."""
        try:
            for timer in channel_timers:
                if timer.message_id not in self.timers:   # 처리 도중 제거된 경우
                    continue
                try:
                    await self._update_one(timer)
                except Exception as e:
                    print(f"[Countdown] 갱신 중 오류 (message_id={timer.message_id}): {e}")
        finally:
            self._running.discard(channel_id)

    @update_loop.before_loop
    async def _before_update_loop(self):
        await self.bot.wait_until_ready()

    async def _update_one(self, timer: Timer):
        now = time.time()
        remaining = math.ceil(timer.end_timestamp - now)
        main = self._partial(timer.channel_id, timer.message_id)

        # 만료: 0:00 표시 → 만든 사람 멘션 → 메모리/DB 삭제
        if remaining <= 0:
            try:
                await main.edit(content=format_clock(0))
            except discord.HTTPException as e:
                print(f"[Countdown] 종료 표시 실패 (message_id={timer.message_id}): {e}")

            # 타이머를 만든 사람 멘션 (그 사람만 알림, 다른 멘션은 전부 차단)
            try:
                channel = self.bot.get_partial_messageable(timer.channel_id)
                await channel.send(
                    END_MESSAGE.format(mention=f"<@{timer.creator_id}>"),
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(id=timer.creator_id)],
                        roles=False,
                        everyone=False,
                        replied_user=False,
                    ),
                )
            except discord.HTTPException as e:
                print(f"[Countdown] 종료 알림 전송 실패 (message_id={timer.message_id}): {e}")

            await self._remove(timer)
            return

        # 진행 중: 시계 갱신
        try:
            t0 = time.time()
            await main.edit(content=format_clock(remaining))
            elapsed = time.time() - t0
            if elapsed > 2:   # 진단용: 수정이 느려진 경우만 로그
                print(f"[Countdown] 느린 수정 감지 (message_id={timer.message_id}): {elapsed:.1f}초")
        except (discord.NotFound, discord.Forbidden) as e:
            # 메시지가 삭제됐거나 접근 불가 → 정보 삭제
            print(f"[Countdown] 메시지를 찾을 수 없어 제거 (message_id={timer.message_id}): {e}")
            await self._remove(timer)
        except discord.HTTPException as e:
            # 일시적인 오류 → 다음 주기에 다시 시도
            print(f"[Countdown] 수정 실패, 다음 주기에 재시도 (message_id={timer.message_id}): {e}")

    # ── /타이머 ───────────────────────────────────────────────────

    @app_commands.command(name="타이머", description="이 채널에서 카운트다운을 시작합니다.")
    @app_commands.rename(seconds="초")
    @app_commands.describe(seconds="카운트다운 시간 (초)")
    @app_commands.guild_only()
    async def timer_command(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 1, MAX_SECONDS],
    ):
        channel = interaction.channel
        if channel is None:
            await interaction.response.send_message("이 채널에서는 사용할 수 없어요.", ephemeral=True)
            return

        # 채널당 타이머 수 제한 (진행 중인 것 + 지금 만드는 중인 것)
        active = sum(1 for t in self.timers.values() if t.channel_id == channel.id)
        reserved = self._creating.get(channel.id, 0)
        if active + reserved >= MAX_TIMERS_PER_CHANNEL:
            await interaction.response.send_message(
                f"이 채널에는 타이머를 최대 {MAX_TIMERS_PER_CHANNEL}개까지만 만들 수 있어요. "
                "진행 중인 타이머가 끝난 뒤에 다시 시도해 주세요.",
                ephemeral=True,
            )
            return

        # 확인과 선점 사이에 await 이 없으므로 동시에 실행돼도 한도를 넘지 않음
        self._creating[channel.id] = reserved + 1
        try:
            await self._create_timer(interaction, channel, seconds)
        finally:
            left = self._creating.get(channel.id, 1) - 1
            if left > 0:
                self._creating[channel.id] = left
            else:
                self._creating.pop(channel.id, None)

    async def _create_timer(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Messageable,
        seconds: int,
    ):
        global countdown_switch

        await interaction.response.defer(ephemeral=True)

        end_ts = int(time.time()) + seconds
        main = None
        try:
            main = await channel.send(format_clock(seconds))
            await db.execute(
                SQL_INSERT, (main.id, channel.id, interaction.user.id, end_ts)
            )
        except Exception as e:
            print(f"[Countdown] 타이머 생성 실패: {e!r}")
            # 만들다 만 메시지 정리
            if main is not None:
                try:
                    await main.delete()
                except Exception:
                    pass
            await interaction.followup.send(
                "타이머를 만들지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
            )
            return

        self.timers[main.id] = Timer(
            message_id=main.id,
            channel_id=channel.id,
            creator_id=interaction.user.id,
            end_timestamp=end_ts,
        )
        countdown_switch = 1

        # "생각 중…" 응답을 지워서 채널에는 카운트다운 메시지만 남김
        try:
            await interaction.delete_original_response()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CountdownCog(bot))