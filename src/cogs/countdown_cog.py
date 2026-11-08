"""
countdown_cog.py
/타이머 {초} — 채널에 이모지 카운트다운 메시지를 만들고 5초마다 갱신

구성
- 원글 (message_id)    : 이모지 시계 메시지. 5초마다 수정된다.
- 답글 (submessage_id) : 원글에 달리는 컨트롤 메시지. "<@만든사람>님의 타이머" + [중지]/[재개] 버튼.
    * [중지] : 카운트다운 정보를 메모리/DB 에서 삭제한다 (메시지는 지우지 않음). 버튼이 [재개]로 바뀐다.
    * [재개] : 원글의 이모지 시계를 읽어 남은 시간을 구하고, 지금 시각 + 남은 시간으로 카운트다운을 다시 만든다.
               버튼이 다시 [중지]로 바뀐다.
    * 버튼은 타이머를 만든 사람만 쓸 수 있다.
    * 타이머가 종료되면 답글의 버튼을 없앤다.
- 중지된 타이머는 DB/메모리에 정보가 없다. 재개에 필요한 정보는 메시지에서 읽는다.
    * 만든 사람 = 답글 내용의 멘션, 원글 = 답글의 reference(답글 대상), 남은 시간 = 원글의 이모지 시계.
- 활성 타이머는 DB(countdowntimer)와 봇 메모리(self.timers)에 같이 보관한다.
  갱신할 때는 DB를 조회하지 않고 메모리만 사용한다.
- 전역 변수 countdown_switch: 0이면 갱신 루프가 아무것도 하지 않고, 1이면 동작.

DB 접근은 db.py(psycopg3 풀 헬퍼)의 fetchall / execute 를 사용한다.
(db.py 에 fetchall 이 추가된 버전이 필요함)
테이블 컬럼: message_id, submessage_id, channel_id, creator_id, end_timestamp
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

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
CONTROL_MESSAGE = "<@{creator_id}>님의 타이머"          # 답글 내용 (여기서 만든 사람을 다시 읽으므로 형식을 바꾸면 안 됨)

# 봇이 재시작해도 버튼이 동작하도록(persistent view) 고정된 custom_id 를 쓴다
CUSTOM_ID_STOP = "countdown:stop"
CUSTOM_ID_RESUME = "countdown:resume"

# 테이블/컬럼 이름이 다르면 여기만 고치면 됨
SQL_SELECT_ALL = (
    "SELECT message_id, submessage_id, channel_id, creator_id, end_timestamp "
    "FROM countdowntimer"
)
SQL_INSERT = (
    "INSERT INTO countdowntimer "
    "(message_id, submessage_id, channel_id, creator_id, end_timestamp) "
    "VALUES (%s, %s, %s, %s, %s)"
)
SQL_DELETE = "DELETE FROM countdowntimer WHERE message_id = %s"

# 카운트다운이 하나라도 있으면 1, 없으면 0
countdown_switch = 0


# ── 표시 형식 ─────────────────────────────────────────────────────

_KEYCAP_RE = re.compile("([0-9])️?⃣")
_MENTION_RE = re.compile(r"<@!?(\d+)>")


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


def parse_clock(content: str) -> int | None:
    """format_clock 의 반대: 이모지 시계 문자열 → 남은 초. 형식이 맞지 않으면 None."""
    if not content or COLON not in content:
        return None
    left, right = content.split(COLON, 1)
    minute_digits = _KEYCAP_RE.findall(left)
    second_digits = _KEYCAP_RE.findall(right)
    if not minute_digits or len(second_digits) != 2:
        return None
    return int("".join(minute_digits)) * 60 + int("".join(second_digits))


# ── 버튼 ──────────────────────────────────────────────────────────

class ControlButton(discord.ui.Button):
    """답글에 붙는 [중지](빨강) / [재개](초록) 버튼. 어느 타이머의 버튼인지는 메시지에서 읽는다."""

    def __init__(self, cog: "CountdownCog", resume: bool):
        super().__init__(
            label="재개" if resume else "중지",
            style=discord.ButtonStyle.success if resume else discord.ButtonStyle.danger,
            custom_id=CUSTOM_ID_RESUME if resume else CUSTOM_ID_STOP,
        )
        self.cog = cog
        self.resume = resume

    async def callback(self, interaction: discord.Interaction):
        if self.resume:
            await self.cog.handle_resume(interaction)
        else:
            await self.cog.handle_stop(interaction)


def make_view(cog: "CountdownCog", resume: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ControlButton(cog, resume))
    return view


@dataclass
class Timer:
    message_id: int               # 원글 (이모지 시계)
    submessage_id: int | None     # 답글 (컨트롤 버튼). 예전 데이터 등은 None 일 수 있음
    channel_id: int
    creator_id: int
    end_timestamp: int            # 유닉스 초


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
        self._views: list[discord.ui.View] = []  # 등록한 persistent view

    # ── 시작/종료 ─────────────────────────────────────────────────

    async def cog_load(self):
        """봇 시작(재부팅) 시 DB를 한 번만 읽어서 메모리에 채우고, 버튼을 등록한다."""
        global countdown_switch

        # 재시작 전에 만든 답글의 버튼도 계속 동작하도록 persistent view 등록
        for resume in (False, True):
            view = make_view(self, resume)
            self.bot.add_view(view)
            self._views.append(view)

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
                submessage_id=r.get("submessage_id"),
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
        for view in self._views:
            view.stop()
        self._views.clear()

    # ── 내부 도우미 ───────────────────────────────────────────────

    def _partial(self, channel_id: int, message_id: int) -> discord.PartialMessage:
        """캐시 없이 메시지를 다루기 위한 PartialMessage."""
        channel = self.bot.get_partial_messageable(channel_id)
        return channel.get_partial_message(message_id)

    async def _remove(self, timer: Timer) -> bool:
        """메모리와 DB에서 카운트다운 정보를 삭제. 메모리가 비면 스위치 off.
        DB 삭제까지 성공하면 True (메모리에서는 항상 지워짐)."""
        global countdown_switch
        self.timers.pop(timer.message_id, None)
        if not self.timers:
            countdown_switch = 0
        try:
            await db.execute(SQL_DELETE, (timer.message_id,))
            return True
        except Exception as e:
            # DB 삭제에 실패해도 메모리에서는 이미 지워짐.
            # (재부팅하면 다시 로드되지만, 이미 만료 상태면 첫 갱신 때 바로 정리됨)
            print(f"[Countdown] DB 삭제 실패 (message_id={timer.message_id}): {e}")
            return False

    def _try_reserve(self, channel_id: int) -> bool:
        """채널당 타이머 수 제한 확인 + 자리 선점. (확인과 선점 사이에 await 이 없어서 동시에 실행돼도 안전)"""
        active = sum(1 for t in self.timers.values() if t.channel_id == channel_id)
        reserved = self._creating.get(channel_id, 0)
        if active + reserved >= MAX_TIMERS_PER_CHANNEL:
            return False
        self._creating[channel_id] = reserved + 1
        return True

    def _release(self, channel_id: int):
        left = self._creating.get(channel_id, 1) - 1
        if left > 0:
            self._creating[channel_id] = left
        else:
            self._creating.pop(channel_id, None)

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
                if timer.message_id not in self.timers:   # 처리 도중 제거된 경우 (중지 등)
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

        # 만료: 0:00 표시 → 답글 버튼 제거 → 만든 사람 멘션 → 메모리/DB 삭제
        if remaining <= 0:
            try:
                await main.edit(content=format_clock(0))
            except discord.HTTPException as e:
                print(f"[Countdown] 종료 표시 실패 (message_id={timer.message_id}): {e}")

            # 답글의 [중지] 버튼 없애기 (답글이 이미 지워졌어도 무시)
            if timer.submessage_id:
                try:
                    await self._partial(timer.channel_id, timer.submessage_id).edit(view=None)
                except discord.HTTPException:
                    pass

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
            # 원글이 삭제됐거나 접근 불가 → 정보 삭제
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
        if not self._try_reserve(channel.id):
            await interaction.response.send_message(
                f"이 채널에는 타이머를 최대 {MAX_TIMERS_PER_CHANNEL}개까지만 만들 수 있어요. "
                "진행 중인 타이머가 끝난 뒤에 다시 시도해 주세요.",
                ephemeral=True,
            )
            return

        try:
            await self._create_timer(interaction, channel, seconds)
        finally:
            self._release(channel.id)

    async def _create_timer(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Messageable,
        seconds: int,
    ):
        global countdown_switch

        await interaction.response.defer(ephemeral=True)

        end_ts = int(time.time()) + seconds
        main = sub = None
        try:
            main = await channel.send(format_clock(seconds))
            # 원글에 컨트롤 답글: 멘션은 표시만 하고 알림은 보내지 않음
            sub = await main.reply(
                CONTROL_MESSAGE.format(creator_id=interaction.user.id),
                view=make_view(self, resume=False),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await db.execute(
                SQL_INSERT, (main.id, sub.id, channel.id, interaction.user.id, end_ts)
            )
        except Exception as e:
            print(f"[Countdown] 타이머 생성 실패: {e!r}")
            # 만들다 만 메시지 정리
            for m in (sub, main):
                if m is not None:
                    try:
                        await m.delete()
                    except Exception:
                        pass
            await interaction.followup.send(
                "타이머를 만들지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True
            )
            return

        self.timers[main.id] = Timer(
            message_id=main.id,
            submessage_id=sub.id,
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

    # ── [중지] / [재개] 버튼 ──────────────────────────────────────

    @staticmethod
    def _read_control(interaction: discord.Interaction) -> tuple[int, int] | None:
        """버튼이 달린 답글에서 (만든 사람 id, 원글 id) 를 읽는다. 읽을 수 없으면 None.
        - 만든 사람: 답글 내용의 멘션
        - 원글: 답글의 reference (답글을 단 대상)
        → 중지된 타이머는 DB/메모리에 정보가 없어도 이 두 값으로 다시 이어갈 수 있다."""
        msg = interaction.message
        if msg is None or msg.reference is None or msg.reference.message_id is None:
            return None
        m = _MENTION_RE.search(msg.content or "")
        if m is None:
            return None
        return int(m.group(1)), msg.reference.message_id

    async def handle_stop(self, interaction: discord.Interaction):
        ctx = self._read_control(interaction)
        if ctx is None:
            await interaction.response.send_message("이 타이머의 정보를 읽을 수 없어요.", ephemeral=True)
            return
        creator_id, main_id = ctx

        if interaction.user.id != creator_id:
            await interaction.response.send_message("타이머를 만든 사람만 사용할 수 있어요.", ephemeral=True)
            return

        timer = self.timers.get(main_id)
        if timer is None:
            await interaction.response.send_message("이미 중지됐거나 끝난 타이머예요.", ephemeral=True)
            return

        remaining = math.ceil(timer.end_timestamp - time.time())
        if remaining <= 0:
            await interaction.response.send_message("곧 끝나는 타이머라 중지할 수 없어요.", ephemeral=True)
            return

        await interaction.response.defer()   # 3초 안에 응답해야 하므로 먼저 확인 응답

        # 메모리 + DB 에서 정보 삭제 (메시지는 지우지 않음)
        db_ok = await self._remove(timer)

        # 원글에 중지한 순간의 남은 시간을 정확히 표시.
        # (마지막 갱신이 최대 몇 초 전일 수 있어서, 이렇게 해야 [재개]가 정확한 시간부터 이어진다)
        try:
            await self._partial(timer.channel_id, main_id).edit(content=format_clock(remaining))
        except discord.HTTPException as e:
            print(f"[Countdown] 중지 시 원글 수정 실패 (message_id={main_id}): {e}")

        # 답글 버튼을 [재개]로
        try:
            await interaction.edit_original_response(view=make_view(self, resume=True))
        except discord.HTTPException as e:
            print(f"[Countdown] 중지 시 답글 수정 실패 (message_id={main_id}): {e}")

        if not db_ok:
            await interaction.followup.send(
                "타이머는 중지했지만 DB 삭제에 실패했어요. 봇이 재시작되면 다시 시작될 수 있어요.",
                ephemeral=True,
            )

    async def handle_resume(self, interaction: discord.Interaction):
        global countdown_switch

        ctx = self._read_control(interaction)
        if ctx is None:
            await interaction.response.send_message("이 타이머의 정보를 읽을 수 없어요.", ephemeral=True)
            return
        creator_id, main_id = ctx

        if interaction.user.id != creator_id:
            await interaction.response.send_message("타이머를 만든 사람만 사용할 수 있어요.", ephemeral=True)
            return

        if main_id in self.timers:
            await interaction.response.send_message("이미 진행 중인 타이머예요.", ephemeral=True)
            return

        channel_id = interaction.channel_id
        if channel_id is None:
            await interaction.response.send_message("이 채널에서는 사용할 수 없어요.", ephemeral=True)
            return

        # 재개해도 채널당 진행 중인 타이머 수 제한은 그대로 적용
        if not self._try_reserve(channel_id):
            await interaction.response.send_message(
                f"이 채널에는 타이머를 최대 {MAX_TIMERS_PER_CHANNEL}개까지만 돌릴 수 있어요. "
                "다른 타이머가 끝난 뒤에 다시 시도해 주세요.",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer()

            # 원글의 이모지 시계를 읽어서 남은 시간을 구한다
            try:
                original = await self._partial(channel_id, main_id).fetch()
            except discord.NotFound:
                await interaction.followup.send("원글이 삭제돼서 재개할 수 없어요.", ephemeral=True)
                return
            except discord.HTTPException as e:
                print(f"[Countdown] 재개 시 원글 읽기 실패 (message_id={main_id}): {e}")
                await interaction.followup.send("원글을 읽지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
                return

            remaining = parse_clock(original.content)
            if not remaining:   # None(형식 오류) 또는 0
                await interaction.followup.send("남은 시간을 읽을 수 없어서 재개할 수 없어요.", ephemeral=True)
                return

            end_ts = int(time.time()) + remaining
            try:
                # 혹시 남아 있을 수 있는 이전 행을 먼저 정리하고 새로 기록
                await db.execute(SQL_DELETE, (main_id,))
                await db.execute(
                    SQL_INSERT, (main_id, interaction.message.id, channel_id, creator_id, end_ts)
                )
            except Exception as e:
                print(f"[Countdown] 재개 시 DB 기록 실패 (message_id={main_id}): {e!r}")
                await interaction.followup.send("타이머를 재개하지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
                return

            self.timers[main_id] = Timer(
                message_id=main_id,
                submessage_id=interaction.message.id,
                channel_id=channel_id,
                creator_id=creator_id,
                end_timestamp=end_ts,
            )
            countdown_switch = 1

            # 답글 버튼을 다시 [중지]로
            try:
                await interaction.edit_original_response(view=make_view(self, resume=False))
            except discord.HTTPException as e:
                print(f"[Countdown] 재개 시 답글 수정 실패 (message_id={main_id}): {e}")
        finally:
            self._release(channel_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(CountdownCog(bot))