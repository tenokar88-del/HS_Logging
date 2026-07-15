"""
logging_cog.py
서버 이벤트 로깅 — 포럼 채널에 날짜별 포스트로 기록
"""

from __future__ import annotations
import io
import os
import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta

# 한국 시간 기준
KST = timezone(timedelta(hours=9))

# 첨부파일 재업로드 관련 설정
# - 디스코드 무료(비-Nitro) 계정 업로드 한도는 2026년 기준 10MB.
#   (2024년 말 25MB에서 10MB로 축소된 뒤 계속 유지 중. 서버가 부스트되어 있거나
#   봇 계정이 Nitro라면 LOG_MAX_ATTACHMENT_BYTES 환경변수로 늘려도 됨: Nitro Basic 50MB, Nitro 500MB,
#   서버 부스트 레벨2 50MB, 레벨3 100MB)
# - 디스코드는 10진법 기준(1MB = 1,000,000바이트)으로 한도를 체크하므로,
#   1024*1024로 계산하면 실제 한도보다 커져서 업로드가 거부될 수 있음. 10진 MB로 계산.
MAX_ATTACHMENT_BYTES = int(os.getenv("LOG_MAX_ATTACHMENT_BYTES", 10_000_000))
# 메시지 하나에 첨부파일이 여러 개 있을 때(최대 10개) 순간 메모리 사용량이
# 과도해지지 않도록 다운로드 총량에도 상한을 둠.
MAX_TOTAL_ATTACHMENT_BYTES = int(os.getenv("LOG_MAX_TOTAL_ATTACHMENT_BYTES", 40_000_000))

# 디스코드 메시지 본문 길이 제한 (안전하게 여유를 조금 둠)
DISCORD_MESSAGE_LIMIT = 1990


def kst_now() -> datetime:
    return datetime.now(KST)


def today_str() -> str:
    return kst_now().strftime("%Y-%m-%d")


def time_str() -> str:
    return kst_now().strftime("%H:%M:%S")


def _load_forum_ids() -> list[int]:
    """
    환경변수에서 포럼 ID 목록을 읽어옴.
    LOG_FORUM_ID_1, LOG_FORUM_ID_2, ... 순서로 탐색.
    단일 서버용 LOG_FORUM_ID도 하위 호환으로 지원.
    """
    ids = []
    # 단일 ID 하위 호환
    single = os.getenv("LOG_FORUM_ID")
    if single:
        ids.append(int(single))
    # 복수 ID
    i = 1
    while True:
        val = os.getenv(f"LOG_FORUM_ID_{i}")
        if not val:
            break
        forum_id = int(val)
        if forum_id not in ids:
            ids.append(forum_id)
        i += 1
    return ids


def _load_source_guild_id() -> int | None:
    """
    로깅 대상 서버(A 서버) ID를 읽어옴.
    SOURCE_GUILD_ID가 설정되지 않으면 None 반환 (모든 서버 허용 — 하위 호환).
    """
    val = os.getenv("SOURCE_GUILD_ID")
    return int(val) if val else None


def _load_optional_int(env_name: str) -> int | None:
    """선택적 환경변수를 int로 읽음. 미설정 시 None."""
    val = os.getenv(env_name)
    return int(val) if val else None


class LoggingCog(commands.Cog):
    # 등업키워드 카테고리 이름 접두사 (예: "등업키워드:호랑이")
    KEYWORD_PREFIX = "등업키워드:"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log_forum_ids: list[int] = _load_forum_ids()
        self.source_guild_id: int | None = _load_source_guild_id()
        # forum_id → 날짜 → 포럼 포스트(Thread) ID 캐시
        self._post_cache: dict[int, dict[str, int]] = {
            fid: {} for fid in self.log_forum_ids
        }

        # 등업키워드 인증 기능 설정
        self.kw_input_channel_id: int | None = _load_optional_int("KW_INPUT_CHANNEL_ID")
        self.kw_category_id: int | None = _load_optional_int("KW_CATEGORY_ID")
        self.role_player_id: int | None = _load_optional_int("ROLE_PLAYER_ID")

    def is_source_guild(self, guild_id: int) -> bool:
        """이벤트가 로깅 대상 서버(A 서버)에서 발생했는지 확인."""
        if self.source_guild_id is None:
            return True  # SOURCE_GUILD_ID 미설정 시 모든 서버 허용 (하위 호환)
        return guild_id == self.source_guild_id

    # ── 포럼 포스트 가져오기 / 생성 ───────────────────────────────

    async def get_or_create_post(self, forum_id: int) -> discord.Thread | None:
        """오늘 날짜 포럼 포스트를 반환. 없으면 생성."""
        today = today_str()
        cache = self._post_cache.setdefault(forum_id, {})

        # 캐시에 있으면 fetch로 확실하게 가져오기 (get_channel은 스레드 반환 불가)
        if today in cache:
            try:
                thread = await self.bot.fetch_channel(cache[today])
                if thread:
                    return thread
            except Exception:
                pass
            del cache[today]

        forum: discord.ForumChannel = self.bot.get_channel(forum_id)
        if not forum or not isinstance(forum, discord.ForumChannel):
            print(f"[Logging] 포럼 채널을 찾을 수 없습니다. ID: {forum_id}")
            return None

        # 활성 포스트 캐시 갱신 후 오늘 날짜 제목 탐색
        try:
            await forum.guild.fetch_active_threads()
        except Exception as e:
            print(f"[Logging] 활성 스레드 갱신 실패: {e}")

        for thread in forum.threads:
            if thread.name == today:
                cache[today] = thread.id
                return thread

        # 없으면 새 포스트 생성
        try:
            new_thread, _ = await forum.create_thread(
                name=today,
                content=f"📋 **{today}** 서버 로그",
            )
            cache[today] = new_thread.id
            return new_thread
        except Exception as e:
            print(f"[Logging] 포스트 생성 실패 (forum_id={forum_id}): {e}")
            return None

    async def log(self, message: str, attachments: list[tuple[str, bytes]] | None = None):
        """등록된 모든 포럼 포스트에 로그 메시지 전송. attachments가 있으면 파일도 함께 재업로드."""
        if len(message) > DISCORD_MESSAGE_LIMIT:
            message = message[: DISCORD_MESSAGE_LIMIT - 1] + "…"
        for forum_id in self.log_forum_ids:
            post = await self.get_or_create_post(forum_id)
            if not post:
                continue
            try:
                if attachments:
                    # forum마다 새 File 객체를 만들어야 함 (discord.File은 재사용 불가)
                    files = [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]
                    await post.send(message, files=files)
                else:
                    await post.send(message)
            except discord.HTTPException as e:
                # 파일 전송이 실패해도(용량 초과 등) 텍스트 로그는 남기기
                print(f"[Logging] 첨부파일 전송 실패, 텍스트만 재시도 (forum_id={forum_id}): {e}")
                try:
                    await post.send(message)
                except Exception as e2:
                    print(f"[Logging] 로그 전송 실패 (forum_id={forum_id}): {e2}")
            except Exception as e:
                print(f"[Logging] 로그 전송 실패 (forum_id={forum_id}): {e}")

    @staticmethod
    def fmt_member(member: discord.Member | discord.User) -> str:
        """닉네임 / 사용자명 / ID — 멘션 없이 표시."""
        display = getattr(member, 'display_name', member.name)
        return f"{display} / `{member.name}` / `{member.id}`"

    @staticmethod
    def fmt_role(role: discord.Role) -> str:
        """역할 멘션 + 이름. 삭제된 역할도 이름이 남아 있으므로 항상 표시."""
        return f"(`{role.name}`)"

    @staticmethod
    def fmt_channel(channel) -> str:
        """채널 멘션 + 이름."""
        return f"<#{channel.id}> (`{channel.name}-{channel.id}`)"

    @staticmethod
    async def _download_attachments(message: discord.Message) -> list[tuple[str, bytes]]:
        """메시지의 첨부파일을 바이트로 다운로드. 개별/총합 용량 초과분은 건너뜀."""
        result: list[tuple[str, bytes]] = []
        total = 0
        for att in message.attachments:
            if att.size and att.size > MAX_ATTACHMENT_BYTES:
                print(f"[Logging] 첨부파일 크기 초과, 건너뜀: {att.filename} ({att.size} bytes)")
                continue
            if total + (att.size or 0) > MAX_TOTAL_ATTACHMENT_BYTES:
                print(f"[Logging] 첨부파일 총합 용량 초과, 나머지 건너뜀: {att.filename}")
                break
            try:
                data = await att.read()
            except Exception as e:
                print(f"[Logging] 첨부파일 다운로드 실패: {att.filename} - {e}")
                continue
            result.append((att.filename, data))
            total += len(data)
        return result

    # ── 등업키워드 인증 ──────────────────────────────────────────

    async def _handle_levelup_keyword(self, message: discord.Message) -> bool:
        """
        KW_INPUT_CHANNEL_ID 채널에 작성된 메시지가
        KW_CATEGORY_ID 카테고리 이름의 '등업키워드:XXXXX' 중 XXXXX와
        완전히 일치하면(엄격 비교) ROLE_PLAYER_ID 역할을 부여.

        반환값: 인증(역할 부여)에 성공했으면 True, 아니면 False.
        """
        if not self.kw_category_id or not self.role_player_id:
            return False

        category = self.bot.get_channel(self.kw_category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            print(f"[Logging] 등업키워드 카테고리를 찾을 수 없습니다. ID: {self.kw_category_id}")
            return False

        if not category.name.startswith(self.KEYWORD_PREFIX):
            print(f"[Logging] 카테고리 이름이 '{self.KEYWORD_PREFIX}' 형식이 아닙니다: {category.name}")
            return False

        keyword = category.name[len(self.KEYWORD_PREFIX):]
        # 엄격한 완전일치: 대소문자, 공백 모두 그대로 비교
        if not keyword or message.content != keyword:
            return False

        role = message.guild.get_role(self.role_player_id)
        if not role:
            print(f"[Logging] 역할을 찾을 수 없습니다. ID: {self.role_player_id}")
            return False

        if role in message.author.roles:
            return True  # 이미 보유 중 — 인증 성공으로 취급

        try:
            await message.author.add_roles(role, reason="등업키워드 인증 성공")
        except discord.Forbidden:
            print(f"[Logging] 역할 부여 권한 없음 (author={message.author.id})")
            return False
        except Exception as e:
            print(f"[Logging] 역할 부여 실패: {e}")
            return False

        await self.log(
            f"🔑 **등업키워드 인증 성공** `{today_str()} {time_str()}`\n"
            f"**멤버:** {self.fmt_member(message.author)}\n"
            f"**부여된 역할:** {self.fmt_role(role)}"
        )
        return True

    # ── 메시지 작성 ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if not self.is_source_guild(message.guild.id):
            return

        # 등업키워드 입력 채널 처리: 인증 시도 후, 관리자가 아니면 메시지 자동 삭제
        if self.kw_input_channel_id and message.channel.id == self.kw_input_channel_id:
            await self._handle_levelup_keyword(message)
            if not message.author.guild_permissions.administrator:
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    print(f"[Logging] 등업키워드 메시지 삭제 권한 없음 (message_id={message.id})")
                except Exception as e:
                    print(f"[Logging] 등업키워드 메시지 삭제 실패: {e}")

        content = (message.content or "*(첨부파일 또는 내용 없음)*")[:1800]

        attachments = await self._download_attachments(message) if message.attachments else []
        attach_note = ""
        if message.attachments:
            attach_note = f"\n**첨부파일:** {len(message.attachments)}개"
            if len(attachments) < len(message.attachments):
                attach_note += f" (다시 업로드됨: {len(attachments)}개, 용량 초과/실패 {len(message.attachments) - len(attachments)}개)"

        await self.log(
            f"💬 **메시지 작성** `{today_str()} {time_str()}`\n"
            f"**채널:** {self.fmt_channel(message.channel)}\n"
            f"**작성자:** {self.fmt_member(message.author)}\n"
            f"**메시지 ID:** `{message.id}`\n"
            f"**내용:** {content}{attach_note}",
            attachments=attachments,
        )

    # ── 메시지 삭제 ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id or not self.is_source_guild(payload.guild_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        channel_str = self.fmt_channel(channel) if channel else f"`{payload.channel_id}`"

        cached = payload.cached_message
        if cached:
            if cached.author.bot:
                return
            content = (cached.content or "*(첨부파일 또는 내용 없음)*")[:1800]
            created_kst = cached.created_at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
            await self.log(
                f"🗑️ **메시지 삭제** `{today_str()} {time_str()}`\n"
                f"**채널:** {channel_str}\n"
                f"**작성자:** {self.fmt_member(cached.author)}\n"
                f"**메시지 ID:** `{payload.message_id}`\n"
                f"**내용:** {content}\n"
                f"**메시지 작성 시간:** `{created_kst}`"
            )
        else:
            await self.log(
                f"🗑️ **메시지 삭제** `{today_str()} {time_str()}`\n"
                f"**채널:** {channel_str}\n"
                f"**메시지 ID:** `{payload.message_id}`\n"
                f"**내용:** *(캐시에 없는 메시지)*"
            )

    # ── 메시지 수정 ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if not payload.guild_id or not self.is_source_guild(payload.guild_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        channel_str = self.fmt_channel(channel) if channel else f"`{payload.channel_id}`"

        cached = payload.cached_message
        if cached:
            if cached.author.bot:
                return
            # 내용이 실제로 바뀐 경우만 로깅
            new_content = payload.data.get("content", "")
            if cached.content == new_content:
                return
            before_content = cached.content[:800]
            after_content = new_content[:800]
            created_kst = cached.created_at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
            await self.log(
                f"✏️ **메시지 수정** `{today_str()} {time_str()}`\n"
                f"**채널:** {channel_str}\n"
                f"**작성자:** {self.fmt_member(cached.author)}\n"
                f"**메시지 ID:** `{payload.message_id}`\n"
                f"**수정 전:** {before_content}\n"
                f"**수정 후:** {after_content}\n"
                f"**메시지 작성 시간:** `{created_kst}`"
            )
        else:
            new_content = payload.data.get("content", "*(알 수 없음)*")[:800]
            # 봇 메시지 여부는 캐시 없으면 확인 불가 — author.bot 체크 생략
            await self.log(
                f"✏️ **메시지 수정** `{today_str()} {time_str()}`\n"
                f"**채널:** {channel_str}\n"
                f"**메시지 ID:** `{payload.message_id}`\n"
                f"**수정 전:** *(캐시에 없는 메시지)*\n"
                f"**수정 후:** {new_content}"
            )

    # ── 멤버 입장 ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self.is_source_guild(member.guild.id):
            return
        await self.log(
            f"📥 **멤버 입장** `{time_str()}`\n"
            f"**멤버:** {self.fmt_member(member)}\n"
            f"**계정 생성일:** {member.created_at.strftime('%Y-%m-%d')}"
        )

    # ── 멤버 퇴장 ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not self.is_source_guild(member.guild.id):
            return
        roles = [r.name for r in member.roles if r.name != "@everyone"]
        roles_str = ", ".join(roles) if roles else "없음"
        await self.log(
            f"📤 **멤버 퇴장** `{time_str()}`\n"
            f"**멤버:** {self.fmt_member(member)}\n"
            f"**보유 역할:** {roles_str}"
        )

    # ── 역할 변경 ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not self.is_source_guild(after.guild.id):
            return
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if not added and not removed:
            return
        lines = [f"🎭 **역할 변경** `{time_str()}`\n**멤버:** {self.fmt_member(after)}"]
        if added:
            lines.append(f"**추가된 역할:** {', '.join(self.fmt_role(r) for r in added)}")
        if removed:
            lines.append(f"**제거된 역할:** {', '.join(self.fmt_role(r) for r in removed)}")
        await self.log("\n".join(lines))

    # ── 채널 생성 ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not self.is_source_guild(channel.guild.id):
            return
        await self.log(
            f"➕ **채널 생성** `{time_str()}`\n"
            f"**채널:** {self.fmt_channel(channel)}\n"
            f"**유형:** {str(channel.type)}"
        )

    # ── 채널 삭제 ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not self.is_source_guild(channel.guild.id):
            return
        await self.log(
            f"➖ **채널 삭제** `{time_str()}`\n"
            f"**채널:** {self.fmt_channel(channel)}\n"
            f"**유형:** {str(channel.type)}"
        )

    # ── 채널 이름 변경 ────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ):
        if not self.is_source_guild(after.guild.id):
            return
        if before.name == after.name:   
            return
        await self.log(
            f"✏️ **채널 이름 변경** `{time_str()}`\n"
            f"**이전 이름:** `{before.name}`\n"
            f"**변경 후:** {self.fmt_channel(after)}"
        )

    # ── 음성채널 입퇴장 ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return
        if not self.is_source_guild(member.guild.id):
            return

        if before.channel is None and after.channel is not None:
            await self.log(
                f"🔊 **음성채널 입장** `{time_str()}`\n"
                f"**멤버:** {self.fmt_member(member)}\n"
                f"**채널:** {self.fmt_channel(after.channel)}"
            )
        elif before.channel is not None and after.channel is None:
            await self.log(
                f"🔇 **음성채널 퇴장** `{time_str()}`\n"
                f"**멤버:** {self.fmt_member(member)}\n"
                f"**채널:** {self.fmt_channel(before.channel)}"
            )
        elif before.channel != after.channel:
            await self.log(
                f"🔀 **음성채널 이동** `{time_str()}`\n"
                f"**멤버:** {self.fmt_member(member)}\n"
                f"**이전:** {self.fmt_channel(before.channel)} → **이후:** {self.fmt_channel(after.channel)}"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))